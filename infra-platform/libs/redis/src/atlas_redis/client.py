"""受拦截器链保护的 RedisClient（c4-model/infra-platform/code-redis.html）。

★ 业务代码和上层原语都经 RedisClient 发命令，不直接操作裸 redis-py 客户端：
  · 单命令（get / set / eval / evalsha / xadd / …）全部经 execute() 穿过拦截器链；
  · 管线（pipeline）收集本批次全部子命令与总载荷大小，在 execute() 时整体过一遍拦截器链；
  · with_policy(fail_open=True) 零拷贝派生新视图，共享同一个底层连接池和同一个熔断器实例；
  · 兼容 atlas_redislock 所需的 set(nx, px) / eval 接口，分布式锁也能天然纳入观测与护栏。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from types import TracebackType
from typing import Any

from .middleware import (
    FAIL_OPEN_FALLBACK,
    CommandContext,
    NextCall,
    RedisMiddleware,
    estimate_payload_bytes,
)

__all__ = ["RedisClient", "TrackedPipeline"]


def _as_int(value: Any) -> Any:
    return value if value is FAIL_OPEN_FALLBACK else int(value)


def _as_bool(value: Any) -> Any:
    return value if value is FAIL_OPEN_FALLBACK else bool(value)


def _compose(middlewares: Sequence[RedisMiddleware], terminal: NextCall) -> NextCall:
    handler = terminal
    for mw in reversed(middlewares):
        prev = handler

        async def _step(
            ctx: CommandContext,
            _mw: RedisMiddleware = mw,
            _next: NextCall = prev,
        ) -> Any:
            return await _mw(ctx, _next)

        handler = _step
    return handler


class TrackedPipeline:
    """受拦截器链保护的 Pipeline 包装器。"""

    def __init__(
        self,
        client: RedisClient,
        *,
        transaction: bool = False,
        key_pattern: str = "",
        fail_open: bool | None = None,
    ) -> None:
        self._client = client
        self._transaction = transaction
        self._key_pattern = key_pattern
        self._fail_open = fail_open
        self._ops: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self._keys: list[str] = []
        self._payload_bytes = 0

    async def __aenter__(self) -> TrackedPipeline:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._ops.clear()

    def _queue(
        self,
        cmd: str,
        *args: Any,
        keys: Sequence[str] = (),
        payload_args: Sequence[Any] = (),
        **kwargs: Any,
    ) -> TrackedPipeline:
        upper = cmd.upper()
        self._ops.append((cmd, args, kwargs))
        self._keys.extend(keys)
        self._payload_bytes += estimate_payload_bytes(upper, payload_args or args)
        return self

    def get(self, key: str) -> TrackedPipeline:
        return self._queue("get", key, keys=(key,))

    def set(
        self,
        key: str,
        value: Any,
        *,
        ex: int | None = None,
        px: int | None = None,
        nx: bool = False,
    ) -> TrackedPipeline:
        return self._queue(
            "set", key, value, keys=(key,), payload_args=(value,), ex=ex, px=px, nx=nx
        )

    def delete(self, *keys: str) -> TrackedPipeline:
        return self._queue("delete", *keys, keys=keys)

    def expire(self, key: str, seconds: int) -> TrackedPipeline:
        return self._queue("expire", key, seconds, keys=(key,))

    def sadd(self, key: str, *values: Any) -> TrackedPipeline:
        return self._queue("sadd", key, *values, keys=(key,), payload_args=values)

    def srem(self, key: str, *values: Any) -> TrackedPipeline:
        return self._queue("srem", key, *values, keys=(key,), payload_args=values)

    def xadd(
        self,
        key: str,
        fields: Mapping[str, Any],
        *,
        maxlen: int | None = None,
        approximate: bool = True,
    ) -> TrackedPipeline:
        kwargs: dict[str, Any] = {}
        if maxlen is not None:
            kwargs["maxlen"] = maxlen
            kwargs["approximate"] = approximate
        return self._queue(
            "xadd",
            key,
            fields,
            keys=(key,),
            payload_args=tuple(fields.values()),
            **kwargs,
        )

    async def execute(self) -> Any:
        if not self._ops:
            return []
        ops = tuple(self._ops)
        subcommands = tuple(cmd.upper() for cmd, _, _ in ops)
        keys = tuple(self._keys)
        payload_bytes = self._payload_bytes
        self._ops.clear()
        self._keys.clear()
        self._payload_bytes = 0

        async def _send(_ctx: CommandContext) -> Any:
            async with self._client.raw.pipeline(transaction=self._transaction) as pipe:
                for cmd, args, kwargs in ops:
                    getattr(pipe, cmd)(*args, **kwargs)
                return await pipe.execute()

        ctx = CommandContext(
            service=self._client.service,
            command="PIPELINE",
            keys=keys,
            key_pattern=self._key_pattern,
            fail_open=(
                self._client.default_fail_open if self._fail_open is None else self._fail_open
            ),
            is_pipeline=True,
            subcommands=subcommands,
            payload_bytes=payload_bytes,
        )
        return await self._client.dispatch(ctx, _send)


class RedisClient:
    """带中间件链的 Redis 客户端门面。"""

    def __init__(
        self,
        raw: Any,
        *,
        service: str,
        middlewares: Sequence[RedisMiddleware] = (),
        default_fail_open: bool = False,
    ) -> None:
        self._raw = raw
        self.service = service
        self._middlewares = tuple(middlewares)
        self.default_fail_open = default_fail_open

    @property
    def raw(self) -> Any:
        """底层 redis-py 客户端（仅供连接池统计、shutdown 或特殊底层集成使用）。"""
        return self._raw

    def with_policy(self, *, fail_open: bool) -> RedisClient:
        """派生一份策略视图，共享底层连接池与熔断器实例。"""
        return RedisClient(
            self._raw,
            service=self.service,
            middlewares=self._middlewares,
            default_fail_open=fail_open,
        )

    async def dispatch(
        self, ctx: CommandContext, terminal: Callable[[CommandContext], Awaitable[Any]]
    ) -> Any:
        chain = _compose(self._middlewares, terminal)
        return await chain(ctx)

    async def execute(
        self,
        command: str,
        *args: Any,
        keys: Sequence[str] = (),
        key_pattern: str = "",
        fail_open: bool | None = None,
        payload_bytes: int = 0,
        _invoker: Callable[[], Awaitable[Any]] | None = None,
    ) -> Any:
        upper = command.upper()
        ctx = CommandContext(
            service=self.service,
            command=upper,
            args=args,
            keys=tuple(keys),
            key_pattern=key_pattern,
            fail_open=self.default_fail_open if fail_open is None else fail_open,
            payload_bytes=payload_bytes,
        )

        async def _terminal(_ctx: CommandContext) -> Any:
            if _invoker is not None:
                return await _invoker()
            return await self._raw.execute_command(upper, *args)

        return await self.dispatch(ctx, _terminal)

    def pipeline(
        self,
        *,
        transaction: bool = False,
        key_pattern: str = "",
        fail_open: bool | None = None,
    ) -> TrackedPipeline:
        return TrackedPipeline(
            self, transaction=transaction, key_pattern=key_pattern, fail_open=fail_open
        )

    # ------------------------------------------------------------------ 常用命令便捷方法

    async def ping(self) -> Any:
        return _as_bool(await self.execute("PING", _invoker=lambda: self._raw.ping()))

    async def get(self, key: str, *, key_pattern: str = "") -> Any:
        return await self.execute(
            "GET",
            key,
            keys=(key,),
            key_pattern=key_pattern,
            _invoker=lambda: self._raw.get(key),
        )

    async def set(
        self,
        key: str,
        value: Any,
        *,
        ex: int | None = None,
        px: int | None = None,
        nx: bool = False,
        key_pattern: str = "",
    ) -> Any:
        kwargs: dict[str, Any] = {}
        if ex is not None:
            kwargs["ex"] = ex
        if px is not None:
            kwargs["px"] = px
        if nx:
            kwargs["nx"] = True
        return await self.execute(
            "SET",
            key,
            value,
            keys=(key,),
            key_pattern=key_pattern,
            payload_bytes=estimate_payload_bytes("SET", (value,)),
            _invoker=lambda: self._raw.set(key, value, **kwargs),
        )

    async def delete(self, *keys: str, key_pattern: str = "") -> Any:
        if not keys:
            return 0
        return _as_int(
            await self.execute(
                "DEL",
                *keys,
                keys=keys,
                key_pattern=key_pattern,
                _invoker=lambda: self._raw.delete(*keys),
            )
        )

    async def exists(self, *keys: str, key_pattern: str = "") -> Any:
        if not keys:
            return 0
        return _as_int(
            await self.execute(
                "EXISTS",
                *keys,
                keys=keys,
                key_pattern=key_pattern,
                _invoker=lambda: self._raw.exists(*keys),
            )
        )

    async def expire(self, key: str, seconds: int, *, key_pattern: str = "") -> Any:
        return _as_bool(
            await self.execute(
                "EXPIRE",
                key,
                seconds,
                keys=(key,),
                key_pattern=key_pattern,
                _invoker=lambda: self._raw.expire(key, seconds),
            )
        )

    async def incr(self, key: str, *, key_pattern: str = "") -> Any:
        return _as_int(
            await self.execute(
                "INCR",
                key,
                keys=(key,),
                key_pattern=key_pattern,
                _invoker=lambda: self._raw.incr(key),
            )
        )

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: Any,
        key_pattern: str = "",
    ) -> Any:
        keys = tuple(str(k) for k in keys_and_args[:numkeys])
        script_args = keys_and_args[numkeys:]
        return await self.execute(
            "EVAL",
            *script_args,
            keys=keys,
            key_pattern=key_pattern,
            payload_bytes=estimate_payload_bytes("EVAL", script_args),
            _invoker=lambda: self._raw.eval(script, numkeys, *keys_and_args),
        )

    async def evalsha(
        self,
        sha: str,
        numkeys: int,
        *keys_and_args: Any,
        key_pattern: str = "",
    ) -> Any:
        keys = tuple(str(k) for k in keys_and_args[:numkeys])
        script_args = keys_and_args[numkeys:]
        return await self.execute(
            "EVALSHA",
            *script_args,
            keys=keys,
            key_pattern=key_pattern,
            payload_bytes=estimate_payload_bytes("EVALSHA", script_args),
            _invoker=lambda: self._raw.evalsha(sha, numkeys, *keys_and_args),
        )

    async def publish(self, channel: str, message: str, *, key_pattern: str = "") -> Any:
        return await self.execute(
            "PUBLISH",
            channel,
            message,
            keys=(channel,),
            key_pattern=key_pattern,
            payload_bytes=estimate_payload_bytes("PUBLISH", (message,)),
            _invoker=lambda: self._raw.publish(channel, message),
        )

    async def xadd(
        self,
        key: str,
        fields: Mapping[str, Any],
        *,
        maxlen: int | None = None,
        approximate: bool = True,
        key_pattern: str = "",
    ) -> Any:
        kwargs: dict[str, Any] = {}
        if maxlen is not None:
            kwargs["maxlen"] = maxlen
            kwargs["approximate"] = approximate
        return await self.execute(
            "XADD",
            key,
            keys=(key,),
            key_pattern=key_pattern,
            payload_bytes=estimate_payload_bytes("XADD", tuple(fields.values())),
            _invoker=lambda: self._raw.xadd(key, fields, **kwargs),
        )

    async def xrange(
        self,
        key: str,
        *,
        min: str = "-",
        max: str = "+",
        count: int | None = None,
        key_pattern: str = "",
    ) -> Any:
        kwargs: dict[str, Any] = {"min": min, "max": max}
        if count is not None:
            kwargs["count"] = count
        return await self.execute(
            "XRANGE",
            key,
            keys=(key,),
            key_pattern=key_pattern,
            _invoker=lambda: self._raw.xrange(key, **kwargs),
        )

    async def xrevrange(
        self,
        key: str,
        *,
        max: str = "+",
        min: str = "-",
        count: int | None = None,
        key_pattern: str = "",
    ) -> Any:
        kwargs: dict[str, Any] = {"max": max, "min": min}
        if count is not None:
            kwargs["count"] = count
        return await self.execute(
            "XREVRANGE",
            key,
            keys=(key,),
            key_pattern=key_pattern,
            _invoker=lambda: self._raw.xrevrange(key, **kwargs),
        )

    async def mget(self, keys: Sequence[str], *, key_pattern: str = "") -> Any:
        if not keys:
            return []
        key_tuple = tuple(keys)
        return await self.execute(
            "MGET",
            *key_tuple,
            keys=key_tuple,
            key_pattern=key_pattern,
            _invoker=lambda: self._raw.mget(key_tuple),
        )

    async def getdel(self, key: str, *, key_pattern: str = "") -> Any:
        return await self.execute(
            "GETDEL",
            key,
            keys=(key,),
            key_pattern=key_pattern,
            _invoker=lambda: self._raw.getdel(key),
        )

    async def getset(self, key: str, value: Any, *, key_pattern: str = "") -> Any:
        """写入新值、返回旧值（沙箱执行记「工作区上次用的沙箱」用它）。

        ★ 是写命令：不在幂等读集合里，断连不重试 —— 重试可能拿回的是自己刚写的值，不是「上一次」。
        """
        return await self.execute(
            "GETSET",
            key,
            value,
            keys=(key,),
            key_pattern=key_pattern,
            payload_bytes=estimate_payload_bytes("GETSET", (value,)),
            _invoker=lambda: self._raw.getset(key, value),
        )

    async def incrbyfloat(self, key: str, amount: float = 1.0, *, key_pattern: str = "") -> Any:
        return await self.execute(
            "INCRBYFLOAT",
            key,
            amount,
            keys=(key,),
            key_pattern=key_pattern,
            _invoker=lambda: self._raw.incrbyfloat(key, amount),
        )

    async def sadd(self, key: str, *values: Any, key_pattern: str = "") -> Any:
        return _as_int(
            await self.execute(
                "SADD",
                key,
                *values,
                keys=(key,),
                key_pattern=key_pattern,
                payload_bytes=estimate_payload_bytes("SADD", values),
                _invoker=lambda: self._raw.sadd(key, *values),
            )
        )

    async def srem(self, key: str, *values: Any, key_pattern: str = "") -> Any:
        return _as_int(
            await self.execute(
                "SREM",
                key,
                *values,
                keys=(key,),
                key_pattern=key_pattern,
                _invoker=lambda: self._raw.srem(key, *values),
            )
        )

    async def smembers(self, key: str, *, key_pattern: str = "") -> Any:
        return await self.execute(
            "SMEMBERS",
            key,
            keys=(key,),
            key_pattern=key_pattern,
            _invoker=lambda: self._raw.smembers(key),
        )

    def scan_iter(self, match: str | None = None, count: int | None = None) -> Any:
        """流式 SCAN 迭代（替代危险的 KEYS 命令）。"""
        return self._raw.scan_iter(match=match, count=count)

    async def xread(
        self,
        streams: Mapping[str, str],
        *,
        count: int | None = None,
        block: int | None = None,
        key_pattern: str = "",
    ) -> Any:
        kwargs: dict[str, Any] = {}
        if count is not None:
            kwargs["count"] = count
        if block is not None:
            kwargs["block"] = block
        return await self.execute(
            "XREAD",
            keys=tuple(streams.keys()),
            key_pattern=key_pattern,
            _invoker=lambda: self._raw.xread(streams, **kwargs),
        )
