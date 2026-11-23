"""Lua 脚本注册中心（c4-model/infra-platform/code-redis.html）。

★ 为什么不能到处写 redis.eval(source, ...)：
  1. 每次调用都把整段 Lua 源码经网络传给 Redis，流式热路径徒增带宽与解析开销；
  2. 脚本散落各处，难以统一统计耗时和排查。
  本模块在本地按 SHA-1 预计算摘要（与 Redis SCRIPT LOAD 一致，无需启动时连网），执行优先走
  EVALSHA；遇到 NOSCRIPT（Redis 重启或 SCRIPT FLUSH）自动回退一次 EVAL 重灌。
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from redis.exceptions import NoScriptError

from .client import RedisClient

__all__ = ["ScriptHandle", "ScriptRegistry"]


def _is_noscript(exc: Exception) -> bool:
    return isinstance(exc, NoScriptError) or "NOSCRIPT" in str(exc).upper()


@dataclass(frozen=True)
class ScriptHandle:
    name: str
    source: str
    sha1: str


class ScriptRegistry:
    """进程级 Lua 脚本注册表。"""

    def __init__(self, client: RedisClient) -> None:
        self._client = client
        self._scripts: dict[str, ScriptHandle] = {}

    def register(self, name: str, lua_source: str) -> ScriptHandle:
        """登记脚本；同名同源码幂等返回，同名不同源码直接报错防止误覆盖。"""
        source = lua_source.strip()
        if not name or not source:
            raise ValueError("脚本名与源码不能为空")
        sha1 = hashlib.sha1(source.encode("utf-8")).hexdigest()
        existing = self._scripts.get(name)
        if existing is not None:
            if existing.sha1 != sha1:
                raise ValueError(f"脚本 {name!r} 已注册且内容不同，请更换脚本名")
            return existing
        handle = ScriptHandle(name=name, source=source, sha1=sha1)
        self._scripts[name] = handle
        return handle

    def get(self, name: str) -> ScriptHandle:
        try:
            return self._scripts[name]
        except KeyError as exc:
            raise KeyError(f"未注册的 Lua 脚本：{name!r}") from exc

    async def run(
        self,
        name: str,
        *,
        keys: Sequence[str] = (),
        args: Sequence[Any] = (),
        client: RedisClient | None = None,
        key_pattern: str = "",
    ) -> Any:
        """优先 EVALSHA，遇 NOSCRIPT 自动回退 EVAL 并由服务端缓存。"""
        handle = self.get(name)
        target = client or self._client
        pattern = key_pattern or f"script:{handle.name}"
        numkeys = len(keys)
        all_args = (*keys, *args)
        try:
            return await target.evalsha(handle.sha1, numkeys, *all_args, key_pattern=pattern)
        except Exception as exc:
            if not _is_noscript(exc):
                raise
            return await target.eval(handle.source, numkeys, *all_args, key_pattern=pattern)
