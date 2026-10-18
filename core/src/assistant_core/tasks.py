"""建任务：校验 → 记账（pending）→ 写 To Do → 记账（done）。

ref = <批次>#<序号>，是确定的；它写在 To Do 备注最后一行。任何一步崩掉，下次都能按 ref
找回：账本 pending 而 To Do 里已有 → 补记 done；To Do 里没有 → 重建。所以同一条不会建两次，
也不会记了账却没建。
"""
from __future__ import annotations

import json
from datetime import date, datetime

from . import dates, policy, todo
from .config import Config
from .ledger import Ledger


class InputError(ValueError):
    """模型给的参数不合格，退回让它改。"""


def _anchor(cfg: Config, kind: str, payload: dict) -> date:
    if kind == 'inbox':
        return datetime.fromisoformat(payload['captured_at']).astimezone(cfg.tz).date()
    return datetime.fromisoformat(payload['date'].replace('Z', '+00:00')).astimezone(cfg.tz).date()


def _source_line(kind: str, payload: dict, where: str, who: str) -> str:
    if kind == 'inbox':
        when = datetime.fromisoformat(payload['captured_at']).strftime('%Y-%m-%d %H:%M')
        parts = [payload.get('source', '截图'), when, where, who]
    else:
        parts = ['邮件', payload.get('date_local', ''), payload.get('from', ''), f"「{payload.get('subject', '')}」"]
    return ' · '.join(policy.escape(p) for p in parts if p)


async def create(cfg: Config, ledger: Ledger, *, batch_id: str, index: int, kind: str, title: str,
                 suggested_list: str, confidence: str, reason: str, quote: str = '', where: str = '',
                 who: str = '', due_date: str | None = None, due_basis: str | None = None,
                 due_explicit: bool = False, important: bool = False, est_minutes: int | None = None,
                 steps: list[str] | None = None, event_time: str | None = None) -> dict:
    batch = ledger.batch(batch_id)
    if batch is None or batch['status'] != 'claimed':
        raise InputError(f'批次 {batch_id} 不在处理中；只能给本轮 inbox_claim / mail_claim 返回的批次建任务')
    if not isinstance(index, int) or index < 1:
        raise InputError('index 从 1 开始，同一批次里每条结果一个序号')
    ref = f'{batch_id}#{index}'
    existing = ledger.item(ref)
    if existing is not None:
        if existing['status'] == 'done':
            return {'ok': True, 'ref': ref, 'status': 'already_created', 'list': existing['list_target'],
                    'title': existing['title']}
        return await recover_one(cfg, ledger, existing)

    if kind not in policy.KINDS:
        raise InputError(f'kind 只能是 {sorted(policy.KINDS)}；不是任务的用 batch_finish 记 none 或 fyi')
    if confidence not in policy.CONFIDENCE:
        raise InputError(f'confidence 只能是 {sorted(policy.CONFIDENCE)}')
    payload = json.loads(batch['payload'])
    anchor = _anchor(cfg, batch['kind'], payload)
    error = dates.check_due(due_date, due_basis, anchor)
    if error:
        raise InputError(error)
    clean = policy.clean_title(kind, title)
    inferred = bool(due_date) and not due_explicit
    target, suggested = policy.choose_list(cfg.lists, kind, suggested_list, confidence, inferred)
    rcfg = cfg.raw['reminders']
    remind = policy.reminder_for(due_date, due_explicit, kind, datetime.now(cfg.tz), rcfg['lead_days'], rcfg['hour'])
    note = policy.compose_note(
        target=target, inbox_list=cfg.lists['inbox'], suggested=suggested,
        source_line=_source_line(batch['kind'], payload, where, who), quote=quote,
        due_date=due_date, due_basis=due_basis, est_minutes=est_minutes, event_time=event_time,
        reason=reason, ref=ref, reminder=remind)
    steps = [policy.escape(s)[:80] for s in (steps or [])][:6]
    detail = {'note': note, 'steps': steps, 'important': bool(important), 'where': where, 'who': who,
              'quote': quote[:300], 'reason': reason, 'due_basis': due_basis, 'est_minutes': est_minutes,
              'source_kind': batch['kind'], 'reminder': remind.strftime('%Y-%m-%d %H:%M') if remind else None}
    ledger.begin_item(ref, batch_id, index, kind, clean, target, suggested, due_date, confidence, detail)
    row = ledger.item(ref)
    result = await _write(cfg, ledger, row)
    return {'ok': True, 'ref': ref, 'status': result, 'list': target, 'suggested_list': suggested,
            'title': clean, 'due_date': due_date, 'reminder': detail['reminder']}


async def _write(cfg: Config, ledger: Ledger, row) -> str:
    detail = json.loads(row['detail'])
    try:
        async with todo.client(cfg) as c:
            task_id = await todo.create_task(c, row['list_target'], row['title'], detail['note'],
                                             due_date=row['due_date'], reminder=detail.get('reminder'),
                                             important=detail.get('important'), steps=detail.get('steps') or None)
    except Exception as e:  # To Do 暂时写不进：账本里留着，之后自动重试
        ledger.fail_item(row['ref'], str(e)[:500])
        return 'queued_for_retry'
    ledger.commit_item(row['ref'], task_id)
    return 'created'


async def recover_one(cfg: Config, ledger: Ledger, row) -> dict:
    try:
        async with todo.client(cfg) as c:
            found = await todo.find_by_ref(c, row['list_target'], row['title'], row['ref'])
    except Exception as e:
        ledger.fail_item(row['ref'], str(e)[:500])
        return {'ok': True, 'ref': row['ref'], 'status': 'queued_for_retry', 'list': row['list_target'],
                'title': row['title']}
    if found:
        ledger.commit_item(row['ref'], found)
        status = 'recovered'
    else:
        status = await _write(cfg, ledger, row)
    return {'ok': True, 'ref': row['ref'], 'status': status, 'list': row['list_target'], 'title': row['title']}


async def recover_all(cfg: Config, ledger: Ledger) -> list[dict]:
    return [await recover_one(cfg, ledger, row) for row in ledger.pending_items()]
