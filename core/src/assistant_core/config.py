"""路径与配置。

项目目录里的 config.json 是给人改的设置；没写的项用这里的默认值。
密钥不放在项目里：DeepSeek key 在 $ASSISTANT_DSH_HOME/.env，Thunderbird token 在
$ASSISTANT_DSH_HOME/assistant-secrets.json，两者权限都是 600。
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT = Path(os.environ.get('ASSISTANT_PROJECT') or Path(__file__).resolve().parents[3])
DSH_HOME = Path(os.environ.get('ASSISTANT_DSH_HOME') or Path.home() / '.dsh-assistant')
STATE = PROJECT / 'state'

DEFAULTS = {
    'timezone': 'Asia/Shanghai',
    # 快捷指令「存储文件」不询问位置时，写到 iCloud 云盘的「快捷指令」文件夹下。
    'inbox_dir': '~/Library/Mobile Documents/iCloud~is~workflow~my~workflows/Documents/个人助理/inbox',
    # 收件箱连续这么多秒没有新文件、文件大小也不再变化，才开始处理（给连续截屏留时间）。
    'quiet_seconds': 45,
    # 前后相隔不超过这么多秒的截图归为一批，一起交给模型理解。
    'batch_gap_seconds': 60,
    'max_image_edge': 2048,
    'lists': {
        'inbox': 'Inbox-ToDo',
        'brief': 'Assistant',
        'allowed': ['Academic-ToDo', 'Students-ToDo', 'Teaching-ToDo', 'Project-ToDo', 'Logistics-ToDo',
                    'Life-ToDo', 'CanWait-ToDo', 'Reading list', 'Idea pool', 'Fun-ToDo'],
        # 影子模式：为空时所有任务都进 Inbox-ToDo。准确率达标后再把清单名加进来直投。
        'direct': [],
    },
    'mail': {
        # 只读 Thunderbird 里加了星标（star）或带任意标签（tag）的邮件。
        # match：flagged = 星标；tagged = 带任意标签；all = 文件夹里的全部（用于 Gmail 的 Starred 文件夹）。
        # 在 config.json 里填，如 {'folder': 'imap://me@example.com@imap.example.com/INBOX', 'match': ['flagged', 'tagged']}；
        # Thunderbird 匹配前会先解码，所以 @ 照写，不要写成 %40。
        'sources': [],
        # Thunderbird 自带的标签（键 → 名字）；自定义标签也会被收进来，只是显示键名。
        'tag_names': {'$label1': '重要', '$label2': '工作', '$label3': '个人', '$label4': '待办', '$label5': '稍后'},
        # 星标常常是后来才加的，所以不按到达时间推进水位，而是每次看最近这么多天的星标邮件，处理过的跳过。
        'lookback_days': 14,
        # 你自己的邮箱：用来认出「你发的」和「你后来回过信」。在 config.json 里填。
        'self_addresses': [],
        'max_per_run': 20,
        'body_chars': 3000,
    },
    'brief': {'reminder_time': '07:30'},
    # 有明确时间节点的任务：截止前 lead_days 天、早上 hour 点在 To Do 里提醒（手机会弹通知）。
    'reminders': {'lead_days': 2, 'hour': 9},
    'retention_days': {'archive': 30, 'failed': 30, 'dsh': 30, 'logs': 90, 'briefs': 90},
    'mstodo_command': '/Applications/To Do MCP.app/Contents/MacOS/mstodo-mcp',
    'mail_bridge': '~/.claude/skills/thunderbird-mail/mail-mcp-bridge.js',
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in over.items():
        out[key] = _merge(base[key], value) if isinstance(value, dict) and isinstance(base.get(key), dict) else value
    return out


def _expand(path: str) -> Path:
    return Path(os.path.expanduser(path))


@dataclass
class Config:
    raw: dict = field(default_factory=dict)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.raw['timezone'])

    @property
    def inbox_dir(self) -> Path:
        return _expand(self.raw['inbox_dir'])

    @property
    def lists(self) -> dict:
        return self.raw['lists']

    @property
    def mail(self) -> dict:
        return self.raw['mail']

    @property
    def mstodo_command(self) -> str:
        return self.raw['mstodo_command']

    @property
    def mail_bridge(self) -> Path:
        return _expand(self.raw['mail_bridge'])

    @property
    def node(self) -> str:
        return shutil.which('node', path='/opt/homebrew/bin:/usr/local/bin:/usr/bin') or 'node'

    @property
    def db_path(self) -> Path:
        return STATE / 'ledger.db'

    def secret(self, name: str) -> str | None:
        if os.environ.get(name):
            return os.environ[name]
        try:
            return json.loads((DSH_HOME / 'assistant-secrets.json').read_text()).get(name)
        except (OSError, ValueError):
            return None


def load() -> Config:
    path = PROJECT / 'config.json'
    over = json.loads(path.read_text()) if path.exists() else {}
    return Config(_merge(DEFAULTS, over))


def state_dir(*parts: str) -> Path:
    path = STATE.joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path
