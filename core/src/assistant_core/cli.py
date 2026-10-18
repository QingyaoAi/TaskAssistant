"""命令行：MCP 服务器入口，以及作业调度器调用的不需要模型的步骤。

    assistant-core serve                 以 stdio 运行 MCP 服务器（dsh profile 里挂这个）
    assistant-core init                  建账本、建 Inbox-ToDo / Assistant 清单
    assistant-core ready inbox|mail      有没有要处理的（0 有，1 没有，3 Thunderbird 不可用）
    assistant-core finalize inbox|mail   作业结束：没处理完的批次放回队列
    assistant-core brief [--print]       生成并发布早上的简报
    assistant-core maintain              重试未写入的任务、清理过期文件、备份账本
    assistant-core status                看看最近的情况
    assistant-core run-start <job> / run-finish <id> <status> [detail]
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, inbox
from .config import DSH_HOME, STATE, state_dir
from .ledger import Ledger

# brief / mail / tasks / todo 会拉进 MCP SDK（很重），只在用到的命令里导入，
# 这样 run-start、run-finish、ready inbox 这些小步骤启动很快。


def _ledger(cfg) -> Ledger:
    return Ledger(cfg.db_path)


def cmd_init(cfg) -> int:
    from . import todo
    led = _ledger(cfg)
    for d in ('processing', 'archive', 'failed', 'briefs', 'logs', 'backups'):
        state_dir(d)
    cfg.inbox_dir.mkdir(parents=True, exist_ok=True)

    async def lists():
        async with todo.client(cfg) as c:
            return await todo.ensure_lists(c, [cfg.lists['inbox'], cfg.lists['brief']])

    created = asyncio.run(lists())
    print(json.dumps({'db': str(cfg.db_path), 'inbox': str(cfg.inbox_dir), 'lists_created': created},
                     ensure_ascii=False))
    return 0


def cmd_ready(cfg, what: str, wait: bool) -> int:
    led = _ledger(cfg)
    if what == 'inbox':
        if led.batches_with_status('inbox', 'released'):
            return 0
        icloud_ready = inbox.wait_quiet(cfg) if wait else any(inbox.scan(cfg, d)[0] for d in inbox.sources(cfg))
        if icloud_ready:
            inbox.stage(cfg)
        return 0 if inbox.scan(cfg)[0] else 1
    from . import mail
    try:
        n = asyncio.run(mail.pending_count(cfg, led))
    except Exception as e:
        print(f'thunderbird unavailable: {e}', file=sys.stderr)
        return 3
    print(n)
    return 0 if n else 1


def cmd_finalize(cfg, what: str) -> int:
    led = _ledger(cfg)
    out = []
    for row in led.batches_with_status(what, 'claimed'):
        status = led.release(row['batch_id'], '作业结束时还没处理完')
        if status == 'failed' and what == 'inbox':
            inbox.move_out(row['batch_id'], 'failed')
        out.append({'batch_id': row['batch_id'], 'status': status})
    print(json.dumps(out, ensure_ascii=False))
    return 0


def _prune(root: Path, days: int) -> int:
    if not root.exists():
        return 0
    cutoff = time.time() - days * 86400
    removed = 0
    for path in sorted(root.rglob('*'), reverse=True):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
            elif path.is_dir() and not any(path.iterdir()) and path != root:
                path.rmdir()
        except OSError:
            pass
    return removed


def cmd_maintain(cfg) -> int:
    from . import tasks
    led = _ledger(cfg)
    report = {'recovered': asyncio.run(tasks.recover_all(cfg, led))}
    inbox.release_stale(led, 'inbox')
    # 已认领但文件已丢（比如手动删了）的批次，直接记失败。
    for row in led.batches_with_status('inbox', 'released'):
        if not (STATE / 'processing' / row['batch_id']).exists():
            led.finish_batch(row['batch_id'], 'failed', summary='原件不见了')
    keep = cfg.raw['retention_days']
    report['pruned'] = {
        'archive': _prune(STATE / 'archive', keep['archive']),
        'failed': _prune(STATE / 'failed', keep['failed']),
        'briefs': _prune(STATE / 'briefs', keep['briefs']),
        'logs': _prune(STATE / 'logs', keep['logs']),
        # read_image 存下的图片副本、会话日志：只在助理自己的 DSH_HOME 里，按天清理。
        'dsh_attachments': _prune(DSH_HOME / 'attachments', keep['dsh']),
        'dsh_sessions': _prune(DSH_HOME / 'sessions', keep['dsh']),
    }
    backups = state_dir('backups')
    led.backup(backups / f"ledger-{datetime.now():%Y%m%d}.db")
    for old in sorted(backups.glob('ledger-*.db'))[:-14]:
        old.unlink()
    print(json.dumps(report, ensure_ascii=False, default=str))
    return 0


def cmd_status(cfg) -> int:
    led = _ledger(cfg)
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec='seconds')
    items = led.since('items', 'created_at', since)
    runs = led.since('runs', 'started_at', since)
    out = {
        'inbox_waiting_files': inbox.waiting(cfg),
        'items_7d': len(items),
        'items_pending': len(led.pending_items()),
        'batches_failed_7d': sum(1 for b in led.since('batches', 'finished_at', since) if b['status'] == 'failed'),
        'runs_7d': {s: sum(1 for r in runs if r['status'] == s) for s in ('ok', 'skipped', 'failed', 'timeout')},
        'last_runs': {job: dict(r) if (r := led.last_run(job)) else None for job in ('inbox', 'mail', 'morning')},
    }
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(__doc__)
        return 2
    cmd, rest = args[0], args[1:]
    if cmd == 'serve':
        from .server import main as serve
        serve()
        return 0
    cfg = config.load()
    if cmd == 'init':
        return cmd_init(cfg)
    if cmd == 'ready' and rest and rest[0] in ('inbox', 'mail'):
        return cmd_ready(cfg, rest[0], '--wait' in rest)
    if cmd == 'finalize' and rest and rest[0] in ('inbox', 'mail'):
        return cmd_finalize(cfg, rest[0])
    if cmd == 'brief':
        from . import brief
        print(asyncio.run(brief.run(cfg, _ledger(cfg), dry_run='--print' in rest)))
        return 0
    if cmd == 'maintain':
        return cmd_maintain(cfg)
    if cmd == 'status':
        return cmd_status(cfg)
    if cmd == 'run-start' and rest:
        print(_ledger(cfg).run_start(rest[0]))
        return 0
    if cmd == 'run-finish' and len(rest) >= 2:
        _ledger(cfg).run_finish(int(rest[0]), rest[1], ' '.join(rest[2:]))
        return 0
    print(__doc__)
    return 2


if __name__ == '__main__':
    sys.exit(main())
