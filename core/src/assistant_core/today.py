"""Today 清单：把今天该处理的任务各复制一条进来，代替 To Do 的「我的一天」（To Do 接口不支持 My Day）。

今天该处理 = 各清单里没完成、截止在 [今天 − overdue_days, 今天 + ahead_days] 之间的任务。
副本只是个入口：
  · 在 Today 里勾掉 → 原任务也标成完成；
  · 在 Today 里删掉 → 今天不再放回来，明天还该处理就再放；
  · 原任务完成了、截止日改了、或被挪到别的清单 → 删掉这条副本（挪了清单的按新位置重建）。
副本记在账本的 today 表里，备注里的 key 用来认回崩溃时漏记的副本。

你自己放进 Today 的任务：放进来那天没做完，第二天第一次对齐时原样移回 Inbox；还在窗口里的，
照常再放一条副本进来。To Do 接口不能挪清单，只能在 Inbox 重建（标题、备注、截止、星标、步骤，
还没响的提醒），核对无误再删 Today 里的；带重复、附件、关联项或类别的建不出原样，留在 Today 并提示。
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta

from . import todo
from .config import Config
from .ledger import Ledger
from .mcpclient import McpError

KEY_MARK = 'today-key: '
# To Do 接口建不出这些，带着的任务移不回 Inbox。
UNMOVABLE = {'repeat': '重复', 'links': '关联项', 'attachments': '附件', 'has_attachments': '附件', 'categories': '类别'}


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


def _steps(t: dict) -> list[dict]:
    return t.get('steps') or []


def _matches(copy: dict, t: dict) -> bool:
    """copy 是不是 t 在 Inbox 里的（可能建到一半的）重建：步骤可以少几条，别的要一样。"""
    def same(x):
        return (x.get('title'), ' '.join((x.get('note') or '').split()), todo.due_of(x), x.get('importance'))
    have, want = [s.get('text') for s in _steps(copy)], [s.get('text') for s in _steps(t)]
    return same(copy) == same(t) and have == want[:len(have)]


async def _move_back(c, today_list: str, inbox: str, task_id: str, now: str) -> str | None:
    """把 Today 里你自己放的任务原样移回 Inbox。返回问题描述；有问题时 Today 里的不动。
    上次崩在中途的，按标题、备注、截止、星标认回 Inbox 里建了的那条，补齐步骤。"""
    t = await todo.get_task(c, today_list, task_id)
    if not t or t.get('completed'):
        return None
    if lost := sorted({v for k, v in UNMOVABLE.items() if t.get(k)}):
        return f"「{t['title']}」带{'、'.join(lost)}，没法原样移回 {inbox}，请手动拖过去"
    copy = None
    for cand in await todo.list_tasks(c, inbox, title_contains=t['title'], limit=20):
        if cand.get('title') == t['title']:
            full = await todo.get_task(c, inbox, cand['id'])
            if _matches(full, t):
                copy = full
                break
    if copy is None:
        reminder = t.get('reminder') if t.get('reminder_on', True) and (t.get('reminder') or '') > now else None
        new_id = await todo.create_task(c, inbox, t['title'], t.get('note') or '', due_date=todo.due_of(t),
                                        reminder=reminder, important=t.get('importance') == 'high',
                                        steps=[s['text'] for s in _steps(t)])
        copy = await todo.get_task(c, inbox, new_id)
        if not _matches(copy, t):
            await todo.delete_own_copy(c, inbox, new_id)
            return f"「{t['title']}」在 {inbox} 里重建得不一样，Today 里的先留着"
    have = _steps(copy)
    for i, step in enumerate(_steps(t)):
        if i >= len(have):
            await todo.add_step(c, inbox, copy['id'], step['text'], checked=bool(step.get('checked')))
        elif step.get('checked') and not have[i].get('checked'):
            await todo.check_step(c, inbox, copy['id'], have[i]['id'])
    await todo.delete_moved(c, today_list, task_id)
    return None


async def refresh(cfg: Config, ledger: Ledger, today: date | None = None) -> dict:
    today = today or datetime.now(cfg.tz).date()
    day = today.isoformat()
    name = cfg.lists['today']
    report = {'added': 0, 'done': 0, 'dismissed': 0, 'removed': 0, 'moved': 0, 'problems': []}
    async with todo.client(cfg) as c:
        await todo.ensure_lists(c, [name])
        in_list = {t['id']: t for t in await todo.list_tasks(c, name, status='all', limit=2000)}

        # 0. 你自己放进 Today、过了那天还没做完的，移回 Inbox。先移再找候选，还该处理的这次就放副本进来。
        own = [t['id'] for t in in_list.values()
               if not t.get('completed') and not note_key(t.get('note')) and not ledger.today_known(t['id'])]
        now = datetime.now(cfg.tz).strftime('%Y-%m-%d %H:%M')
        for task_id, seen in ledger.today_own_seen(own, day).items():
            if seen >= day:
                continue
            try:
                problem = await _move_back(c, name, cfg.lists['inbox'], task_id, now)
            except McpError as e:
                problem = f"「{in_list[task_id]['title']}」没能移回 {cfg.lists['inbox']}：{str(e)[:100]}"
            if problem:
                report['problems'].append(problem)
            else:
                report['moved'] += 1

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
             f"移出 {report['removed']}", f"移回 Inbox {report['moved']}"]
    return '，'.join(parts) + ''.join(f'；{p}' for p in report['problems'])
