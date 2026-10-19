"""Microsoft To Do：通过 mstodo MCP 只做建、读、改。唯一的删除是 Today 清单里助理自己建的副本。"""
from __future__ import annotations

from .config import Config
from .mcpclient import McpClient


def client(cfg: Config) -> McpClient:
    return McpClient('mstodo', cfg.mstodo_command, timeout=90)


def _task_id(result) -> str:
    if isinstance(result, dict):
        for key in ('id', 'task_id'):
            if result.get(key):
                return result[key]
        if isinstance(result.get('task'), dict):
            return _task_id(result['task'])
    raise ValueError(f'To Do 没有返回任务 id：{str(result)[:200]}')


async def create_task(c: McpClient, list_name: str, title: str, note: str, due_date: str | None = None,
                      reminder: str | None = None, important: bool | None = None,
                      steps: list[str] | None = None) -> str:
    args = {'list': list_name, 'title': title, 'note': note}
    if due_date:
        args['due_date'] = due_date
    if reminder:
        args['reminder'] = reminder
    if important:
        args['important'] = True
    if steps:
        args['steps'] = steps
    return _task_id(await c.call('create_task', args))


async def list_tasks(c: McpClient, list_name: str, status: str = 'open', title_contains: str | None = None,
                     limit: int = 300) -> list[dict]:
    args = {'list': list_name, 'status': status, 'limit': limit}
    if title_contains:
        args['title_contains'] = title_contains
    result = await c.call('list_tasks', args)
    return result.get('tasks', []) if isinstance(result, dict) else []


async def get_task(c: McpClient, list_name: str, task_id: str) -> dict:
    result = await c.call('get_task', {'list': list_name, 'task_id': task_id})
    return result.get('task', result) if isinstance(result, dict) else {}


async def update_task(c: McpClient, list_name: str, task_id: str, **fields) -> None:
    await c.call('update_task', {'list': list_name, 'task_id': task_id, **fields})


async def delete_own_copy(c: McpClient, list_name: str, task_id: str) -> None:
    """只给 today.py 删 Today 清单里助理自己建的副本用；别的任务一律不删。"""
    await c.call('delete_task', {'list': list_name, 'task_id': task_id})


async def list_names(c: McpClient) -> list[str]:
    result = await c.call('list_lists')
    lists = result if isinstance(result, list) else result.get('lists', []) if isinstance(result, dict) else []
    return [item['name'] for item in lists]


async def ensure_lists(c: McpClient, names: list[str]) -> list[str]:
    existing = {n.lower() for n in await list_names(c)}
    created = []
    for name in names:
        if name.lower() not in existing:
            await c.call('create_list', {'name': name})
            created.append(name)
    return created


async def find_by_ref(c: McpClient, list_name: str, title: str, ref: str) -> str | None:
    """按备注最后一行的 ref 找已经建好的任务（崩溃恢复用）。"""
    probe = title[:12]
    for task in await list_tasks(c, list_name, status='all', title_contains=probe, limit=50):
        note = task.get('note') or ''
        if f'ref: {ref}' in note:
            return task['id']
        if task.get('title', '').startswith(probe):
            full = await get_task(c, list_name, task['id'])
            if f'ref: {ref}' in (full.get('note') or ''):
                return task['id']
    return None


def due_of(task: dict) -> str | None:
    for key in ('due_date', 'due', 'dueDate'):
        value = task.get(key)
        if isinstance(value, dict):
            value = value.get('dateTime') or value.get('date')
        if value:
            return str(value)[:10]
    return None
