#!/usr/bin/python3
"""安装 / 卸载个人助理的 launchd 作业。

    launchd.py install     写 ~/Library/LaunchAgents/local.taskassistant.*.plist 并加载
    launchd.py uninstall   卸载并删除这些 plist（数据都保留）
    launchd.py status      看加载状态

作业：
  inbox     收件箱有变化就运行（WatchPaths），另每 10 分钟兜底
  mail      每 30 分钟（时间窗口由 run.py 判断：工作日 8–22 点，周末每 2 小时）
  morning   每天 07:00 生成简报（Mac 睡着就醒来后补跑一次）
  maintain  每天 04:30 重试、清理、备份
"""
import os
import plistlib
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
AGENTS = Path.home() / 'Library' / 'LaunchAgents'
INBOX = Path.home() / 'Library/Mobile Documents/iCloud~is~workflow~my~workflows/Documents/个人助理/inbox'
LABEL = 'local.taskassistant.{}'
JOBS = {
    # 连「个人助理」根目录一起监听：iCloud 同名冲突或旧版快捷指令可能把文件存在那里。
    'inbox': {'WatchPaths': [str(INBOX), str(INBOX.parent)], 'StartInterval': 600},
    'mail': {'StartInterval': 1800, 'RunAtLoad': True},
    'morning': {'StartCalendarInterval': {'Hour': 7, 'Minute': 0}},
    'maintain': {'StartCalendarInterval': {'Hour': 4, 'Minute': 30}},
}


LAUNCHER = Path.home() / 'Applications' / 'TaskAssistant.app' / 'Contents' / 'MacOS' / 'TaskAssistant'


def plist(job: str) -> dict:
    logs = PROJECT / 'state' / 'logs' / job
    logs.mkdir(parents=True, exist_ok=True)
    # 经「TaskAssistant.app」启动：iCloud 云盘的隐私授权给这个 app 一次即可（见 jobs/launcher）。
    program = [str(LAUNCHER), job] if LAUNCHER.exists() else ['/usr/bin/python3', str(PROJECT / 'jobs' / 'run.py'), job]
    return {
        'Label': LABEL.format(job),
        'ProgramArguments': program,
        'WorkingDirectory': str(PROJECT),
        'EnvironmentVariables': {'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'},
        'StandardOutPath': str(logs / 'launchd.out'),
        'StandardErrorPath': str(logs / 'launchd.err'),
        # Background 会被系统压低 I/O 优先级，Python 加载依赖要几十秒；作业很短，用 Standard。
        'ProcessType': 'Standard',
        'ThrottleInterval': 30,
        **JOBS[job],
    }


def launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(['launchctl', *args], capture_output=True, text=True)


def install() -> None:
    AGENTS.mkdir(parents=True, exist_ok=True)
    INBOX.mkdir(parents=True, exist_ok=True)
    domain = f'gui/{os.getuid()}'
    for job in JOBS:
        path = AGENTS / f'{LABEL.format(job)}.plist'
        launchctl('bootout', f'{domain}/{LABEL.format(job)}')
        with open(path, 'wb') as f:
            plistlib.dump(plist(job), f)
        r = launchctl('bootstrap', domain, str(path))
        print(f'{job:9s} {"已加载" if r.returncode == 0 else "加载失败：" + r.stderr.strip()}')


def uninstall() -> None:
    domain = f'gui/{os.getuid()}'
    for job in JOBS:
        launchctl('bootout', f'{domain}/{LABEL.format(job)}')
        path = AGENTS / f'{LABEL.format(job)}.plist'
        if path.exists():
            path.unlink()
        print(f'{job:9s} 已卸载')


def status() -> None:
    for job in JOBS:
        r = launchctl('print', f'gui/{os.getuid()}/{LABEL.format(job)}')
        if r.returncode != 0:
            print(f'{job:9s} 未加载')
            continue
        state = next((l.strip() for l in r.stdout.splitlines() if l.strip().startswith('state =')), '')
        last = next((l.strip() for l in r.stdout.splitlines() if 'last exit code' in l), '')
        print(f'{job:9s} {state}  {last}')


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    {'install': install, 'uninstall': uninstall, 'status': status}.get(cmd, lambda: print(__doc__))()
