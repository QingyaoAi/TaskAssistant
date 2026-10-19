#!/usr/bin/python3
"""作业调度器：launchd 按时间或文件变化调用。

    run.py inbox      收件箱有截图就交给模型处理（等收件箱安静 45 秒再开始）
    run.py mail       有新邮件就交给模型处理（工作日 8–22 点；周末每 2 小时最多一次）
    run.py morning    把今天该处理的任务放进 Today 清单，简报只存本机（纯代码）
    run.py maintain   重试、清理、备份（纯代码）
    加 --force 跳过时间窗口。

每个作业一把锁，互不阻塞；项目目录下有 PAUSE 文件时什么都不做。超时会连同子进程一起结束。
出错时弹一条系统通知，细节在 state/logs/<作业>/ 里。只用标准库（系统自带的 python3 即可）。
"""
import fcntl
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
STATE = PROJECT / 'state'
DSH_HOME = Path.home() / '.dsh-assistant'
CORE = PROJECT / 'core' / '.venv' / 'bin' / 'assistant-core'
DSH = '/opt/homebrew/bin/dsh'
PATH = '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'
TIMEOUT = {'inbox': 600, 'mail': 900, 'morning': 300, 'maintain': 900}
PROMPT = {
    'inbox': '运行作业 inbox：先读取 skill「assistant-inbox」，然后严格按它处理收件箱，直到 inbox_claim 返回空。',
    'mail': '运行作业 mail：先读取 skill「assistant-mail」，然后严格按它处理新邮件，直到 mail_claim 返回空。',
}
MAX_LOOPS = 3
TODAY_EVERY = 30 * 60  # 收件箱、邮件作业顺带对齐 Today 清单的最短间隔（建了新任务时马上对）


def stamp() -> str:
    return datetime.now().strftime('%Y%m%d-%H%M%S')


def log(job: str, msg: str) -> None:
    d = STATE / 'logs' / job
    d.mkdir(parents=True, exist_ok=True)
    line = f'{datetime.now():%Y-%m-%d %H:%M:%S} {msg}'
    with open(d / 'runner.log', 'a') as f:
        f.write(line + '\n')
    print(line, file=sys.stderr)


def read_key():
    try:
        for line in (DSH_HOME / '.env').read_text().splitlines():
            if line.startswith('DEEPSEEK_API_KEY='):
                return line.split('=', 1)[1].strip()
    except OSError:
        pass
    return None


def child_env() -> dict:
    # 去掉会串到别的 dsh 环境的变量，再设助理自己的。
    env = {k: v for k, v in os.environ.items() if not k.startswith('DSH_') and k != 'DEEPSEEK_API_KEY'}
    env.update(PATH=PATH, DSH_HOME=str(DSH_HOME), DSH_AGENTS_HOME=str(DSH_HOME / 'agents'),
               DSH_PERMISSION_MODE='read-only', LANG='zh_CN.UTF-8', PYTHONUNBUFFERED='1')
    key = read_key()
    if key:
        env['DEEPSEEK_API_KEY'] = key
    return env


def core(*args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run([str(CORE), *args], capture_output=True, text=True, env=child_env(), cwd=PROJECT,
                          timeout=timeout)


def notify(text: str) -> None:
    script = f'display notification {text!r} with title "个人助理"'.replace("'", '"')
    subprocess.run(['osascript', '-e', script], capture_output=True, timeout=10)


def in_window(job: str) -> bool:
    if job != 'mail':
        return True
    now = datetime.now()
    if now.weekday() < 5:
        return 8 <= now.hour < 22
    if not 9 <= now.hour < 22:
        return False
    marker = STATE / 'locks' / 'mail.weekend'
    if marker.exists() and time.time() - marker.stat().st_mtime < 115 * 60:
        return False
    marker.touch()
    return True


def run_dsh(job: str) -> tuple:
    d = STATE / 'logs' / job
    d.mkdir(parents=True, exist_ok=True)
    s = stamp()
    with open(d / f'{s}.jsonl', 'w') as out, open(d / f'{s}.err', 'w') as err:
        proc = subprocess.Popen([DSH, '--profile', 'collect', '--json', PROMPT[job]], stdout=out, stderr=err,
                                env=child_env(), cwd=PROJECT, start_new_session=True)
        try:
            rc = proc.wait(timeout=TIMEOUT[job])
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
            return 'timeout', f'超过 {TIMEOUT[job]} 秒，日志 {s}'
    # dsh 会把推理过程以「dsh: reasoning」打到 stderr，那不是错误；MCP 没挂上会有
    # 「dsh: warning: … did not activate」，那是错误。
    errors = [line for line in (d / f'{s}.err').read_text(errors='replace').splitlines()
              if (line.startswith('dsh:') and not line.startswith('dsh: reasoning')) or 'Error' in line[:40]]
    if rc != 0 or errors:
        return 'failed', f'exit {rc}；{errors[-1][:300] if errors else ""}；日志 {s}'
    return 'ok', f'日志 {s}'


def finalize(job: str) -> int:
    """没处理完的批次放回队列；返回放回的个数。"""
    result = core('finalize', job)
    return result.stdout.count('"batch_id"')


def sync_today(force: bool) -> str:
    """把你在 Today 里勾掉的同步回原任务、把新到期的放进来。"""
    marker = STATE / 'locks' / 'today.sync'
    if not force and marker.exists() and time.time() - marker.stat().st_mtime < TODAY_EVERY:
        return ''
    marker.touch()
    r = core('today', timeout=300)
    return f'Today：{r.stdout.strip()[-200:]}' if r.returncode == 0 else f'Today 对齐出错：{r.stderr.strip()[-200:]}'


def work(job: str) -> tuple:
    if job == 'morning':
        today = core('today', timeout=TIMEOUT[job])
        (STATE / 'locks' / 'today.sync').touch()
        core('brief', timeout=TIMEOUT[job])  # 只存本机，出错不影响 Today
        if today.returncode != 0:
            return 'failed', today.stderr.strip()[-300:]
        return 'ok', f'Today：{today.stdout.strip()[-300:]}'
    if job == 'maintain':
        r = core('maintain', timeout=TIMEOUT[job])
        return ('ok', r.stdout.strip()[-300:]) if r.returncode == 0 else ('failed', r.stderr.strip()[-300:])
    status, details = 'skipped', []
    for loop in range(MAX_LOOPS):
        ready = core('ready', job, *(['--wait'] if job == 'inbox' else []), timeout=400)
        if ready.returncode == 3:
            return ('skipped' if loop == 0 else status), 'thunderbird 不可用：' + ready.stderr.strip()[-200:]
        if ready.returncode != 0:
            return status, '；'.join(details) or '没有要处理的'
        status, detail = run_dsh(job)
        left = finalize(job)
        details.append(detail + (f'；{left} 批没处理完，已放回队列' if left else ''))
        if status != 'ok':
            break
    return status, '；'.join(details)


def main(argv: list) -> int:
    if not argv or argv[0] not in TIMEOUT:
        print(__doc__)
        return 2
    job, force = argv[0], '--force' in argv
    if (PROJECT / 'PAUSE').exists():
        log(job, '已暂停（项目目录下有 PAUSE 文件），跳过')
        return 0
    if not force and not in_window(job):
        return 0
    (STATE / 'locks').mkdir(parents=True, exist_ok=True)
    lock = open(STATE / 'locks' / f'{job}.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log(job, '上一次还在运行，跳过')
        return 0
    run_id = core('run-start', job).stdout.strip() or '0'
    try:
        status, detail = work(job)
    except Exception as e:  # noqa: BLE001 —— 调度器自己出错也要记下来并通知
        status, detail = 'failed', f'{type(e).__name__}: {e}'
    if job in ('inbox', 'mail'):
        try:
            note = sync_today(force=status == 'ok')
        except Exception as e:  # noqa: BLE001 —— Today 没对上不算作业失败，下次再对
            note = f'Today 对齐出错：{type(e).__name__}: {e}'
        detail = '；'.join(p for p in (detail, note) if p)
    core('run-finish', run_id, status, detail)
    log(job, f'{status} {detail}')
    if status in ('failed', 'timeout'):
        notify(f'{job} 作业{"超时" if status == "timeout" else "出错"}：{detail[:80]}')
    return 0 if status in ('ok', 'skipped') else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
