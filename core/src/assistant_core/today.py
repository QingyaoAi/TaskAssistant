"""Today 清单：把今天该处理的任务各复制一条进来，代替 To Do 的「我的一天」（To Do 接口不支持 My Day）。

今天该处理 = 各清单里没完成、截止在 [今天 − overdue_days, 今天 + ahead_days] 之间的任务。
副本只是个入口：
  · 在 Today 里勾掉 → 原任务也标成完成；
  · 在 Today 里删掉 → 今天不再放回来，明天还该处理就再放；
  · 原任务完成了、截止日改了、或被挪到别的清单 → 删掉这条副本（挪了清单的按新位置重建）。
副本记在账本的 today 表里，备注里的 key 用来认回崩溃时漏记的副本。只删自己建的副本，
你自己加进 Today 的任务不碰。
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta

from . import todo
from .config import Config
from .ledger import Ledger
from .mcpclient import McpError

KEY_MARK = 'today-key: '


def key_of(list_name: str, task_id: str) -> str:
    return f'{list_name}#{hashlib.sha1(task_id.encode()).hexdigest()[:10]}'


def label(due: str, today: date) -> str:
    days = (date.fromisoformat(due) - today).days
    if days < 0:
        return f'已过期 {-days} 天'
    return {0: '今天截止', 1: '明天截止', 2: '后天截止'}.get(days, f'{due[5:]} 截止')


def mirror_title(src: dict, today: date) -> str:
    return f"{src['title']} · {label(src['due'], today)}"


def mirror_note(src: dict) -> str:
    # list_tasks 只返回备注前 200 字，key 要在这之内。
    return f"在这里勾掉，原任务（{src['list']}）也会标成完成；删掉 = 今天先不看。\n{KEY_MARK}{src['key']}"


def note_key(note: str | None) -> str | None:
    for line in (note or '').splitlines():
        if line.startswith(KEY_MARK):
            return line[len(KEY_MARK):].strip()
    return None


async def _candidates(c, cfg: Config, today: date) -> tuple[dict[str, dict], set[str]]:
    """返回 (key → 原任务, 没读成的清单)。没读成的清单里的副本这次先不动。"""
    window = cfg.raw['today']
    lo = (today - timedelta(days=window['overdue_days'])).isoformat()
    hi = (today + timedelta(days=window['ahead_days'])).isoformat()
    found, failed = {}, set()
    for name in [*cfg.lists['allowed'], cfg.lists['inbox']]:
        try:
            tasks = await todo.list_tasks(c, name, status='open', limit=500)
        except Exception:
            failed.add(name)
            continue
        for t in tasks:
            due = todo.due_of(t)
            if due and lo <= due <= hi:
                key = key_of(name, t['id'])
                found[key] = {'key': key, 'list': name, 'id': t['id'], 'title': t.get('title', ''), 'due': due}
    return found, failed


async def _complete_source(c, row, cands: dict[str, dict]) -> str | None:
    """把副本对应的原任务标成完成。原任务被挪走时按标题在候选里找唯一的一条。返回问题描述。"""
    try:
        await todo.update_task(c, row['src_list'], row['src_id'], completed=True)
        cands.pop(row['key'], None)
        return None
    except McpError:
        pass
    same = [s for s in cands.values() if s['title'] == row['title']]
    if len(same) == 1:
        await todo.update_task(c, same[0]['list'], same[0]['id'], completed=True)
        cands.pop(same[0]['key'])
        return None
    return f"没找到「{row['title']}」的原任务，请在原清单里手动勾掉"


async def _delete(c, list_name: str, mirror_id: str) -> None:
    try:
        await todo.delete_own_copy(c, list_name, mirror_id)
    except McpError:  # 已经不在了
        pass


async def refresh(cfg: Config, ledger: Ledger, today: date | None = None) -> dict:
    today = today or datetime.now(cfg.tz).date()
    day = today.isoformat()
    name = cfg.lists['today']
    report = {'added': 0, 'done': 0, 'dismissed': 0, 'removed': 0, 'problems': []}
    async with todo.client(cfg) as c:
        await todo.ensure_lists(c, [name])
        in_list = {t['id']: t for t in await todo.list_tasks(c, name, status='all', limit=2000)}
        cands, failed = await _candidates(c, cfg, today)

        # 1. 你在 Today 里勾掉或删掉的
        for row in ledger.today_open():
            t = in_list.get(row['mirror_id'])
            if t is None:
                try:
                    t = await todo.get_task(c, name, row['mirror_id'])
                except McpError:
                    t = None
            if not t:
                ledger.today_close(row['mirror_id'], 'dismissed', day)
                report['dismissed'] += 1
            elif t.get('completed'):
                problem = await _complete_source(c, row, cands)
                if problem:
                    report['problems'].append(problem)
                ledger.today_close(row['mirror_id'], 'done', day)
                report['done'] += 1

        # 2. 认回上次建了却没记进账本的副本
        for t in in_list.values():
            key = note_key(t.get('note'))
            if key and not t.get('completed') and not ledger.today_known(t['id']):
                src = cands.get(key)
                if src:
                    ledger.today_add(t['id'], src, day)
                elif key.split('#')[0] not in failed:
                    await _delete(c, name, t['id'])

        # 3. 已有的副本：还该处理就更新标题，不该了就删掉
        open_keys = set()
        for row in ledger.today_open():
            src = cands.get(row['key'])
            if src:
                open_keys.add(row['key'])
                title = mirror_title(src, today)
                current = in_list.get(row['mirror_id'], {}).get('title')
                if current is not None and current != title:
                    try:
                        await todo.update_task(c, name, row['mirror_id'], title=title)
                    except McpError:  # 刚好被你删了，下次按「删掉」处理
                        pass
            elif row['src_list'] not in failed:
                await _delete(c, name, row['mirror_id'])
                ledger.today_close(row['mirror_id'], 'removed', day)
                report['removed'] += 1

        # 4. 新的。To Do 默认新建的排在上面，所以截止晚的先建，最急的在最上面。
        skip = ledger.today_closed_keys(day)
        for key, src in sorted(cands.items(), key=lambda kv: kv[1]['due'], reverse=True):
            if key in open_keys or key in skip:
                continue
            mirror_id = await todo.create_task(c, name, mirror_title(src, today), mirror_note(src))
            ledger.today_add(mirror_id, src, day)
            report['added'] += 1
    return report


def summary(report: dict) -> str:
    parts = [f"放进 {report['added']}", f"勾掉 {report['done']}", f"你删了 {report['dismissed']}",
             f"移出 {report['removed']}"]
    return '，'.join(parts) + ''.join(f'；{p}' for p in report['problems'])
