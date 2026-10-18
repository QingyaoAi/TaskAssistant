#!/usr/bin/env python3
"""把助理需要的两个密钥复制进 ~/.dsh-assistant（权限 600），不打印任何值。

- DEEPSEEK_API_KEY：取自你平时用的 ~/.dsh/.credentials.yaml（dsh 网页「设置 → 模型」存的那个），
  写进 ~/.dsh-assistant/.env。headless 不读凭据存储，作业调度器从这里读出后放进启动环境。
- MAIL_MCP_TOKEN：取自 ~/.dsh/profiles/web/cordis.patch.yml 里 Thunderbird 那一项，
  写进 ~/.dsh-assistant/assistant-secrets.json，由 assistant-core 启动邮件桥时使用。

key 换了以后重新运行一次即可。
"""
import json
import os
import re
import sys
from pathlib import Path

HOME = Path(os.environ.get('ASSISTANT_DSH_HOME') or Path.home() / '.dsh-assistant')
USER_DSH = Path(os.environ.get('ASSISTANT_USER_DSH_HOME') or Path.home() / '.dsh')


def read_ref(path: Path, name: str):
    try:
        text = path.read_text()
    except OSError:
        return None
    in_refs = False
    for line in text.split('\n'):
        if re.match(r'^\S', line):
            in_refs = line.strip() == 'refs:'
        elif in_refs:
            m = re.match(r'^ {2}([^\s:][^:]*):\s*(.+)$', line)
            if m and m.group(1) == name:
                value = m.group(2).strip()
                if value[:1] in '"\'':
                    value = json.loads(value) if value[0] == '"' else value[1:-1]
                return value or None
    return None


def read_mail_token(path: Path):
    try:
        text = path.read_text()
    except OSError:
        return None
    m = re.search(r"MAIL_MCP_TOKEN:\s*['\"]?([^'\"\s]+)", text)
    return m.group(1) if m else None


def write_private(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(content)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def main() -> int:
    HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    ok = True
    key = read_ref(USER_DSH / '.credentials.yaml', 'DEEPSEEK_API_KEY')
    if key:
        write_private(HOME / '.env', f'DEEPSEEK_API_KEY={key}\n')
        print('DEEPSEEK_API_KEY：已写入', HOME / '.env')
    else:
        ok = False
        print('DEEPSEEK_API_KEY：没找到。先在 dsh 网页「设置 → 模型」里填好 key，再运行一次。')
    token = read_mail_token(USER_DSH / 'profiles' / 'web' / 'cordis.patch.yml')
    if token:
        write_private(HOME / 'assistant-secrets.json', json.dumps({'MAIL_MCP_TOKEN': token}) + '\n')
        print('MAIL_MCP_TOKEN：已写入', HOME / 'assistant-secrets.json')
    else:
        ok = False
        print('MAIL_MCP_TOKEN：没找到（web profile 里没有 Thunderbird MCP）。邮件作业会跳过。')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
