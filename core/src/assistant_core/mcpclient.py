"""以客户端身份调用别的 MCP 服务器（To Do、Thunderbird 桥）。

模型看不到这些服务器：assistant-core 只把自己挑过的几种操作交给模型，删除之类的能力
根本不经过这里。
"""
from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class McpError(RuntimeError):
    pass


class McpClient:
    def __init__(self, name: str, command: str, args: list[str] | None = None, env: dict | None = None,
                 timeout: float = 120):
        self.name, self.command, self.args, self.env, self.timeout = name, command, args or [], env, timeout
        self._stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None

    async def __aenter__(self) -> 'McpClient':
        self._stack = AsyncExitStack()
        try:
            params = StdioServerParameters(command=self.command, args=self.args, env=self.env)
            read, write = await self._stack.enter_async_context(stdio_client(params))
            self.session = await self._stack.enter_async_context(ClientSession(read, write))
            await asyncio.wait_for(self.session.initialize(), 60)
        except BaseException as e:
            await self._stack.aclose()
            raise McpError(f'{self.name} 启动失败：{e}') from e
        return self

    async def __aexit__(self, *exc) -> None:
        if self._stack:
            await self._stack.aclose()

    async def call(self, tool: str, args: dict | None = None, timeout: float | None = None):
        assert self.session is not None
        try:
            result = await asyncio.wait_for(self.session.call_tool(tool, args or {}), timeout or self.timeout)
        except asyncio.TimeoutError as e:
            raise McpError(f'{self.name}.{tool} 超时') from e
        text = '\n'.join(c.text for c in result.content if getattr(c, 'type', '') == 'text')
        # 不同版本的 SDK 字段名不同（isError / is_error）。
        if getattr(result, 'is_error', None) or getattr(result, 'isError', None):
            raise McpError(f'{self.name}.{tool}: {text[:500]}')
        data = getattr(result, 'structured_content', None) or getattr(result, 'structuredContent', None)
        if data:
            # FastMCP 风格的服务器会把非对象结果包在 {"result": ...} 里。
            return data.get('result', data) if isinstance(data, dict) and set(data) == {'result'} else data
        try:
            return json.loads(text)
        except ValueError:
            return text
