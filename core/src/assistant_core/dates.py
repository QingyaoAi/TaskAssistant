"""日期：给模型的日历表，以及截止日与依据的一致性检查（纯函数，有单测）。

模型换算「周五前」「下周一」时最容易错一天，所以每批数据都附上从基准日起三周的
日期—星期对照表；模型写回的截止日如果和依据里的星期对不上，就退回让它改。
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

WEEKDAYS = '一二三四五六日'
_WEEKDAY_RE = re.compile(r'(?:周|星期|礼拜)([一二三四五六日天])')
_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def weekday_cn(d: date) -> str:
    return '周' + WEEKDAYS[d.weekday()]


def calendar_table(anchor: date, days: int = 21) -> str:
    """从基准日前一天起列出 days 天，标出今天、本周、下周，供模型对照。"""
    monday = anchor - timedelta(days=anchor.weekday())
    rows = []
    for i in range(-1, days):
        d = anchor + timedelta(days=i)
        week = (d - monday).days // 7
        tag = {0: '本周', 1: '下周', 2: '下下周'}.get(week, '')
        mark = '（基准日）' if i == 0 else ''
        rows.append(f'{d.isoformat()} {weekday_cn(d)} {tag}{mark}')
    return '\n'.join(rows)


def parse_date(text: str) -> date:
    if not _DATE_RE.match(text or ''):
        raise ValueError(f'日期必须是 YYYY-MM-DD：{text!r}')
    return date.fromisoformat(text)


def check_due(due: str | None, basis: str | None, anchor: date) -> str | None:
    """返回错误说明；没问题返回 None。"""
    if not due:
        return None
    try:
        d = parse_date(due)
    except ValueError as e:
        return str(e)
    if not (basis or '').strip():
        return 'due_date 有值时必须写 due_basis（原文怎么说、以哪天为基准）'
    if d < anchor - timedelta(days=7):
        return f'截止日 {due} 比基准日 {anchor} 早一周以上，多半算错了；没有把握就不要填截止日'
    if d > anchor + timedelta(days=400):
        return f'截止日 {due} 离基准日 {anchor} 超过一年，多半算错了'
    named = {('日' if ch == '天' else ch) for ch in _WEEKDAY_RE.findall(basis)}
    if named and WEEKDAYS[d.weekday()] not in named:
        return (f'依据里说的是{"/".join("周" + n for n in sorted(named, key=WEEKDAYS.index))}，'
                f'但 {due} 是{weekday_cn(d)}；请对照日历表重新换算')
    return None


def local_date(dt: datetime, tz) -> date:
    return dt.astimezone(tz).date()
