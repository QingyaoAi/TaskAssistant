"""邮件：通过 Thunderbird 桥，只取你在 Thunderbird 里加了星标（star）或带任意标签（tag）的邮件。

星标常常是收到之后才加的，所以不按到达时间推进水位：每次看最近 lookback_days 天里符合条件的
邮件，账本里已经有的跳过。同一个会话只处理最新的那一封，往来记录作为上下文一起交给模型，
避免一个长会话建出一串重复任务。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr

from .config import Config
from .inbox import release_stale
from .ledger import Ledger
from .mcpclient import McpClient, McpError


def client(cfg: Config) -> McpClient:
    token = cfg.secret('MAIL_MCP_TOKEN')
    if not token:
        raise McpError('缺少 MAIL_MCP_TOKEN（应在 ~/.dsh-assistant/assistant-secrets.json）')
    return McpClient('thunderbird', cfg.node, [str(cfg.mail_bridge)], env={'MAIL_MCP_TOKEN': token}, timeout=90)


def batch_id_for(msg: dict) -> str:
    key = msg.get('messageId') or msg.get('id') or json.dumps(msg, sort_keys=True)
    return 'mail-' + hashlib.sha1(key.encode()).hexdigest()[:16]


def thread_key(msg: dict) -> str:
    account = (msg.get('folder') or msg.get('id') or '').split('/')[2:3]
    return f"{account[0] if account else ''}#{msg.get('threadId') or msg.get('messageId') or msg.get('id')}"


# IMAP / Thunderbird 的系统关键字，不是用户打的标签（Gmail 几乎每封都带 nonjunk）。
SYSTEM_KEYWORDS = {'junk', 'nonjunk', 'notjunk', '$junk', '$notjunk', '$nonjunk', '$forwarded', 'forwarded',
                   '$mdnsent', '$submitpending', '$phishing', '$replied', '$important', '$notphishing', '$label0'}


def real_tags(cfg: Config, tags) -> list[str]:
    """用户打的标签（显示名）。"""
    return [cfg.mail['tag_names'].get(t, t) for t in (tags or []) if t.lower() not in SYSTEM_KEYWORDS]


def _addr(author: str) -> str:
    return parseaddr(author or '')[1].lower()


def _when(msg: dict) -> datetime:
    return datetime.fromisoformat(msg['date'].replace('Z', '+00:00'))


def pick_per_thread(msgs: list[dict]) -> tuple[list[dict], list[dict]]:
    """每个会话只留最新的一封。返回 (要看的, 同会话里较早的)。同一封邮件出现在多个来源里只算一次。"""
    unique: dict[str, dict] = {}
    for m in msgs:
        unique.setdefault(m.get('messageId') or m['id'], m)
    newest: dict[str, dict] = {}
    for m in unique.values():
        key = thread_key(m)
        if key not in newest or _when(m) > _when(newest[key]):
            newest[key] = m
    keep = sorted(newest.values(), key=_when)
    keep_ids = {id(m) for m in keep}
    return keep, [m for m in unique.values() if id(m) not in keep_ids]


async def _search(c: McpClient, cfg: Config) -> list[dict]:
    after = (datetime.now(timezone.utc) - timedelta(days=cfg.mail['lookback_days'])).isoformat(timespec='seconds')
    found = []
    for src in cfg.mail['sources']:
        for match in src.get('match', ['all']):
            base = {'folder': src['folder'], 'after': after, 'sort': 'date', 'limit': 200}
            if match == 'flagged':
                queries = [dict(base, flagged=True)]
            elif match == 'tagged':
                # 自带标签逐个查；再扫最近的邮件，把带自定义标签的也收进来。
                queries = [dict(base, tag=key) for key in cfg.mail['tag_names']] + [base]
            else:
                queries = [base]
            for i, args in enumerate(queries):
                try:
                    result = await c.call('search_mail', args)
                except McpError as e:
                    if 'no folder matches' in str(e):
                        break  # 这个文件夹在 Thunderbird 里没有（比如没订阅），跳过
                    raise
                msgs = [m for m in (result.get('messages', []) if isinstance(result, dict) else []) if m.get('date')]
                if match == 'tagged' and 'tag' not in args:
                    msgs = [m for m in msgs if real_tags(cfg, m.get('tags'))]
                found += msgs
    return found


async def _candidates(c: McpClient, cfg: Config, ledger: Ledger) -> list[dict]:
    """要交给模型的邮件：每个会话最新的一封，且账本里还没有。同会话较早的记为跳过。"""
    keep, older = pick_per_thread(await _search(c, cfg))
    for m in older:
        bid = batch_id_for(m)
        if ledger.batch(bid) is None:
            ledger.mark_skipped(bid, 'mail', _meta(m), '同一会话里有更新的邮件')
    return [m for m in keep if ledger.batch(batch_id_for(m)) is None]


def _meta(msg: dict) -> dict:
    return {k: msg.get(k) for k in ('id', 'messageId', 'subject', 'author', 'date', 'folderName', 'threadId')}


async def pending_count(cfg: Config, ledger: Ledger) -> int:
    n = len(ledger.batches_with_status('mail', 'released', 'claimed'))
    async with client(cfg) as c:
        n += len(await _candidates(c, cfg, ledger))
    return n


def _body(message: dict) -> str:
    for key in ('body', 'text', 'plainBody', 'content'):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ''


def _thread_tasks(ledger: Ledger, key: str) -> list[str]:
    """同一会话以前已经建过的任务标题。"""
    rows = ledger.db.execute(
        "SELECT i.title FROM items i JOIN batches b ON b.batch_id = i.batch_id "
        "WHERE b.kind='mail' AND json_extract(b.payload, '$.thread_key') = ?", (key,)).fetchall()
    return [r[0] for r in rows]


async def _payload(c: McpClient, cfg: Config, ledger: Ledger, msg: dict) -> dict:
    full = await c.call('get_message', {'id': msg['id'], 'includeBody': True})
    full = full.get('message', full) if isinstance(full, dict) else {}
    body = re.sub(r'\n\s*\n+', '\n\n', _body(full)).strip()
    limit = cfg.mail['body_chars']
    me = {a.lower() for a in cfg.mail['self_addresses']}
    thread, replied = [], False
    try:
        t = await c.call('get_thread', {'id': msg['id'], 'includeBodies': False})
        for e in (t.get('messages', []) if isinstance(t, dict) else [])[-8:]:
            when = _when(e).astimezone(cfg.tz).strftime('%m-%d %H:%M') if e.get('date') else ''
            thread.append(f"{when} {e.get('author', '')}: {e.get('subject', '')}".strip())
            if e.get('date') and _when(e) > _when(msg) and _addr(e.get('author', '')) in me:
                replied = True
    except McpError:
        pass
    key = thread_key(msg)
    attachments = full.get('attachments') or []
    return {
        'batch_id': batch_id_for(msg),
        'id': msg['id'],
        'messageId': msg.get('messageId'),
        'thread_key': key,
        'from': msg.get('author', ''),
        'from_me': _addr(msg.get('author', '')) in me,
        'to': msg.get('recipients', ''),
        'cc': msg.get('ccList', ''),
        'subject': msg.get('subject', ''),
        'date': msg['date'],
        'date_local': _when(msg).astimezone(cfg.tz).strftime('%Y-%m-%d %H:%M'),
        'flagged': bool(msg.get('flagged')),
        'tags': real_tags(cfg, msg.get('tags')),
        'attachments': [a.get('name') if isinstance(a, dict) else str(a) for a in attachments][:10],
        'body': body[:limit] + ('……（后略）' if len(body) > limit else ''),
        'thread': thread,
        'replied_by_me_later': replied,
        'tasks_already_in_thread': _thread_tasks(ledger, key),
    }


def view(payload: dict) -> dict:
    return {k: v for k, v in payload.items() if k not in ('id', 'messageId', 'thread_key', 'date')}


async def claim(cfg: Config, ledger: Ledger, limit: int) -> list[dict]:
    release_stale(ledger, 'mail')
    out = []
    for row in ledger.batches_with_status('mail', 'released')[:limit]:
        payload = json.loads(row['payload'])
        ledger.upsert_claim(row['batch_id'], 'mail', payload)
        out.append(view(payload))
    if len(out) >= limit:
        return out
    async with client(cfg) as c:
        for msg in (await _candidates(c, cfg, ledger))[: limit - len(out)]:
            payload = await _payload(c, cfg, ledger, msg)
            ledger.upsert_claim(payload['batch_id'], 'mail', payload)
            out.append(view(payload))
    return out
