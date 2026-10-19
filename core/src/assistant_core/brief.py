"""每天早上的简报。纯代码汇总，不调用模型。

只存在本机 state/briefs/（assistant-core brief --print 可以看），不再发到 To Do、不提醒；
今天该处理的任务改由 today.py 放进 Today 清单。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from . import dates, inbox, todo
from .config import Config, state_dir
from .ledger import Ledger

BRIEF_MARK = '☀️'


def _since_iso(hours: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec='seconds')


def _src(detail: dict) -> str:
    who = detail.get('who') or ''
    where = detail.get('where') or ''
    kind = '邮件' if detail.get('source_kind') == 'mail' else '截图'
    return '，'.join(p for p in (kind, where, who) if p)


async def gather(cfg: Config, ledger: Ledger) -> dict:
    today = datetime.now(cfg.tz).date()
    horizon = (today + timedelta(days=cfg.raw['reminders']['lead_days'])).isoformat()
    due, soon, starred = [], [], []
    inbox_open = []
    async with todo.client(cfg) as c:
        for name in [*cfg.lists['allowed'], cfg.lists['inbox']]:
            try:
                tasks = await todo.list_tasks(c, name, status='open', limit=500)
            except Exception:
                continue
            for t in tasks:
                d = todo.due_of(t)
                if d and d <= today.isoformat():
                    due.append((d, name, t.get('title', '')))
                elif d and d <= horizon:
                    soon.append((d, name, t.get('title', '')))
                elif t.get('importance') == 'high' and not d:
                    starred.append((name, t.get('title', '')))
                if name == cfg.lists['inbox']:
                    inbox_open.append(t.get('title', ''))
    since = _since_iso(24)
    new_items = [r for r in ledger.since('items', 'created_at', since)]
    finished = ledger.since('batches', 'finished_at', since)
    fyi_lines = [b['summary'] for b in finished if b['outcome'] == 'fyi' and b['summary']]
    failed = [b for b in finished if b['status'] == 'failed']
    bad_runs = [r for r in ledger.since('runs', 'started_at', since) if r['status'] in ('failed', 'timeout')]
    tb_down = [r for r in ledger.since('runs', 'started_at', since)
               if r['job'] == 'mail' and r['status'] == 'skipped' and 'thunderbird' in (r['detail'] or '').lower()]
    return {'today': today, 'due': sorted(due), 'soon': sorted(soon), 'starred': starred[:5], 'inbox_open': inbox_open,
            'new_items': new_items, 'fyi': fyi_lines, 'failed': failed,
            'bad_runs': bad_runs, 'tb_down': bool(tb_down), 'waiting': inbox.waiting(cfg),
            'skipped_mail': sum(1 for b in finished if b['status'] == 'skipped')}


def compose(cfg: Config, data: dict) -> tuple[str, str]:
    today = data['today']
    title = f"{BRIEF_MARK} {today:%m/%d} {dates.weekday_cn(today)} 今日简报"
    out = []
    overdue = [d for d in data['due'] if d[0] < today.isoformat()]
    due_today = [d for d in data['due'] if d[0] == today.isoformat()]
    out.append(f'今天到期 {len(due_today)} 条' + (f'，已过期 {len(overdue)} 条' if overdue else ''))
    for d, name, t in due_today[:10]:
        out.append(f'  · {t}（{name}）')
    for d, name, t in overdue[:8]:
        out.append(f'  · [过期 {d[5:]}] {t}（{name}）')
    if data['soon']:
        out.append(f"未来 {cfg.raw['reminders']['lead_days']} 天到期 {len(data['soon'])} 条：")
        for d, name, t in data['soon'][:10]:
            day = dates.weekday_cn(datetime.fromisoformat(d).date())
            out.append(f'  · {d[5:]} {day} {t}（{name}）')
    if data['starred']:
        out.append('星标、无截止：')
        out += [f'  · {t}（{name}）' for name, t in data['starred']]
    items = data['new_items']
    out.append('')
    out.append(f'过去 24 小时新收集 {len(items)} 条')
    inbox_list = cfg.lists['inbox']
    direct = [r for r in items if r['list_target'] != inbox_list]
    shadow = [r for r in items if r['list_target'] == inbox_list]
    if direct:
        out.append('  已进清单：')
        out += [f"    · {r['title']}（{r['list_target']}{'，' + r['due_date'][5:] if r['due_date'] else ''}）"
                for r in direct]
    if shadow:
        out.append(f'  待确认（{inbox_list}）：')
        for r in shadow:
            detail = json.loads(r['detail'])
            hint = f"建议 {r['list_suggested']}" if r['list_suggested'] else '没把握归哪'
            due = f"，{r['due_date'][5:]}" if r['due_date'] else ''
            status = '' if r['status'] == 'done' else '，⚠ 还没写进 To Do，稍后重试'
            out.append(f"    · {r['title']}（{hint}{due}；{_src(detail)}{status}）")
    if data['fyi']:
        out.append('只需知道：')
        out += [f'  · {line}' for line in data['fyi'][:5]]
    problems = []
    for b in data['failed']:
        problems.append(f"处理失败：{b['batch_id']}（{b['summary'] or '原因不明'}，原件在 state/failed）")
    for r in data['bad_runs']:
        problems.append(f"{r['job']} 作业{'超时' if r['status'] == 'timeout' else '出错'}（{r['started_at'][5:16]}）")
    if data['tb_down']:
        problems.append('Thunderbird 有段时间没开着，那段时间的邮件会在它打开后补扫')
    if data['waiting']:
        problems.append(f"收件箱还有 {data['waiting']} 个文件等待处理（Mac 睡眠或 iCloud 还没同步完）")
    if problems:
        out.append('')
        out.append('需要留意：')
        out += [f'  · {p}' for p in problems]
    out.append('')
    out.append(f"待确认清单里共有 {len(data['inbox_open'])} 条；拖到对应清单 = 接受，删掉 = 不是任务。")
    return title, '\n'.join(out)


async def run(cfg: Config, ledger: Ledger, dry_run: bool = False) -> str:
    data = await gather(cfg, ledger)
    title, text = compose(cfg, data)
    path = state_dir('briefs') / f"{data['today'].isoformat()}.md"
    path.write_text(f'# {title}\n\n{text}\n')
    return f'{title}\n\n{text}' if dry_run else f'saved: {path.name}'
