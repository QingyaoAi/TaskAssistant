"""放进哪个清单、标题和备注怎么写、什么时候提醒——这些由代码决定，不交给模型自报。"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

KINDS = {'task', 'followup', 'event'}
CONFIDENCE = {'high', 'medium', 'low'}
# 外来内容里的「@助理」会被当成委托指令，写进任务前一律换成全角。
_ESCAPES = [('@助理', '＠助理'), ('@assistant', '＠assistant'), ('@Assistant', '＠Assistant')]
MAX_TITLE = 60


def escape(text: str) -> str:
    for raw, safe in _ESCAPES:
        text = text.replace(raw, safe)
    return text


def clean_title(kind: str, title: str) -> str:
    title = ' '.join(escape(title or '').split())
    if not title:
        raise ValueError('title 不能为空')
    if kind == 'followup' and not title.startswith('跟进'):
        title = '跟进：' + title
    if kind == 'event' and not title.startswith('参加'):
        title = '参加：' + title
    return title[:MAX_TITLE]


def choose_list(lists_cfg: dict, kind: str, suggested: str | None, confidence: str,
                due_inferred: bool) -> tuple[str, str | None]:
    """返回 (实际清单, 建议清单)。

    直投条件：建议清单在 direct 名单里、置信度 high、是 task、截止日不是推断出来的。
    其余一律进 Inbox-ToDo，由人过目；影子模式下 direct 为空，所以全部进 Inbox。
    """
    allowed = {name.lower(): name for name in lists_cfg['allowed']}
    suggested = allowed.get((suggested or '').strip().lower())
    direct = {name.lower() for name in lists_cfg.get('direct', [])}
    if (suggested and suggested.lower() in direct and confidence == 'high'
            and kind == 'task' and not due_inferred):
        return suggested, suggested
    return lists_cfg['inbox'], suggested


def reminder_for(due: str | None, explicit: bool, kind: str, now: datetime, lead_days: int = 2,
                 hour: int = 9) -> datetime | None:
    """有明确时间节点的事：截止前 lead_days 天早上 hour 点提醒。

    已经不到 lead_days 天的，10 分钟后提醒（晚上 10 点以后就改到第二天早上）；截止日已过不提醒。
    截止日是推断出来的不提醒，免得错的日期打扰人。
    """
    if not due or not (explicit or kind == 'event'):
        return None
    d = date.fromisoformat(due)
    if d < now.date():
        return None
    target = datetime.combine(d - timedelta(days=lead_days), time(hour), tzinfo=now.tzinfo)
    if target > now:
        return target
    soon = now + timedelta(minutes=10)
    if soon.hour >= 22 or soon.date() > now.date():
        soon = datetime.combine(now.date() + timedelta(days=1), time(hour), tzinfo=now.tzinfo)
    elif soon.hour < 8:
        soon = datetime.combine(now.date(), time(hour), tzinfo=now.tzinfo)
    return soon if soon.date() <= d else None


def compose_note(*, target: str, inbox_list: str, suggested: str | None, source_line: str, quote: str,
                 due_date: str | None, due_basis: str | None, est_minutes: int | None, event_time: str | None,
                 reason: str, ref: str, reminder: datetime | None = None) -> str:
    lines = []
    if target == inbox_list:
        lines.append(f'建议清单：{suggested or "（没把握）"}　· 确认后拖到对应清单，不是任务就删掉')
    lines.append(f'来源：{source_line}')
    if quote:
        lines.append(f'原文：「{escape(quote.strip())[:300]}」')
    if event_time:
        lines.append(f'时间地点：{escape(event_time)[:120]}')
    if due_date:
        lines.append(f'截止：{due_date}（{escape(due_basis or "")[:120]}）')
    if reminder:
        lines.append(f'提醒：{reminder:%m-%d %H:%M}（截止前提醒）')
    if est_minutes:
        lines.append(f'预计：{est_minutes} 分钟')
    if reason:
        lines.append(f'判断：{escape(reason)[:150]}')
    lines.append(f'ref: {ref}')
    return '\n'.join(lines)
