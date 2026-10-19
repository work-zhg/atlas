"""native agent 的 MCP 工具装配（MCP 详设 §06）。

装配期：从目录缓存构造 BaseTool，零网络。缺什么明确失败（§13.2）。
调用期：一条 interceptor 链（外 → 内）：

  ① identity_headers   X-Atlas-Run/Thread/User —— 审计用，不是授权凭据
  ② errors_to_result   传输错误 / 超时 / 401 → isError 结果交给模型
  ③ call_timeout       asyncio.timeout 包住真正的网络调用
  ④ clip_result        超大文本结果掐头去尾

② 必须在 ③ 外面，否则接不住超时。

engine 不认识 MCP：这里产出的就是普通 BaseTool，名字已是模型侧名
（server__tool），审批中间件按名字匹配自然生效。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from ...domain.events import EventType
from ...domain.mcp_naming import parse_tool_id, tool_id
from ...errors import CapabilityUnavailable
from .catalog import McpCatalog, ServerSnapshot, describe_error
from .config import connection_for
from .review import ReviewIndex, review_status

logger = logging.getLogger(__name__)

Handler = Callable[[Any], Awaitable[Any]]

_RETRY_HINT = "可稍后重试，或换一种方式完成任务。"


class MissingTool(CapabilityUnavailable):
    """spec 里勾了某个 MCP 工具，但目标 server 上没有它（或它不可用）。"""


@dataclass(frozen=True)
class CallContext:
    """闭包捕获，不进工具参数 schema —— 与 search_memory 的 user_id 同一条纪律：
    一旦身份可由模型指定，就是一个冒用他人身份的口子。"""

    run_id: UUID
    thread_id: UUID
    user_id: UUID


# ---------------------------------------------------------------- interceptors


def identity_headers(ctx: CallContext) -> Any:
    headers = {
        "X-Atlas-Run": str(ctx.run_id),
        "X-Atlas-Thread": str(ctx.thread_id),
        "X-Atlas-User": str(ctx.user_id),
    }

    async def intercept(request: Any, handler: Handler) -> Any:
        return await handler(request.override(headers={**(request.headers or {}), **headers}))

    return intercept


def _error_result(text: str) -> Any:
    from mcp.types import CallToolResult, TextContent

    return CallToolResult(content=[TextContent(type="text", text=text)], isError=True)


def _leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for sub in exc.exceptions for leaf in _leaves(sub)]
    return [exc]


def _transport_message(server: str, tool: str, exc: BaseException, timeout_s: float) -> str | None:
    """属于传输层的异常 → 给模型的说明；其余返回 None（照旧冒泡）。

    ★ 不能写成 except Exception：吞掉 CancelledError 会让取消失效；吞掉编程
      错误会让 bug 伪装成「服务不可用」被模型绕过去，永远不会被发现。
    """
    import httpx
    from mcp.shared.exceptions import McpError

    leaves = _leaves(exc)
    if any(isinstance(e, asyncio.CancelledError) for e in leaves):
        return None
    for e in leaves:
        if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (401, 403):
            return (
                f"MCP 服务 {server} 拒绝了认证（HTTP {e.response.status_code}），"
                "请告知用户联系管理员检查该服务的凭据。不要重试。"
            )
    if all(isinstance(e, TimeoutError) for e in leaves):
        return f"MCP 服务 {server} 调用 {tool} 超时（{timeout_s:g}s）。{_RETRY_HINT}"
    if all(isinstance(e, (httpx.HTTPError, McpError, TimeoutError, OSError)) for e in leaves):
        return f"MCP 服务 {server} 暂时不可用（{describe_error(exc)}）。{_RETRY_HINT}"
    return None


def errors_to_result(server: str, timeout_s: float) -> Any:
    async def intercept(request: Any, handler: Handler) -> Any:
        try:
            return await handler(request)
        except BaseException as exc:
            message = _transport_message(server, request.name, exc, timeout_s)
            if message is None:
                raise
            logger.warning(
                "MCP 调用失败 server=%s tool=%s：%s", server, request.name, describe_error(exc)
            )
            return _error_result(message)

    return intercept


def call_timeout(timeout_s: float) -> Any:
    async def intercept(request: Any, handler: Handler) -> Any:
        async with asyncio.timeout(timeout_s):
            return await handler(request)

    return intercept


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head, tail = int(limit * 0.7), int(limit * 0.3)
    omitted = len(text) - head - tail
    return f"{text[:head]}\n\n…[结果过长，省略中间 {omitted} 字符]…\n\n{text[-tail:]}"


def clip_result(max_chars: int) -> Any:
    """没开文件工具的 agent 没有 FilesystemMiddleware 的转存保护，一个返回
    5 MB JSON 的调用会直接撑爆上下文。这是最后一道。

    ★ 阈值必须高于 FilesystemMiddleware 的转存阈值（20000 token × 4 = 80 000
      字符）：本 interceptor 在工具内部执行、早于中间件看到结果，阈值更低就会
      抢先裁掉本可完整转存到工作区的内容。
    """

    async def intercept(request: Any, handler: Handler) -> Any:
        from mcp.types import CallToolResult, TextContent

        result = await handler(request)
        if not isinstance(result, CallToolResult):
            return result
        content = [
            TextContent(type="text", text=_clip(c.text, max_chars))
            if isinstance(c, TextContent) and len(c.text) > max_chars
            else c
            for c in result.content
        ]
        return result.model_copy(update={"content": content})

    return intercept


# ---------------------------------------------------------------- 装配


def wanted_tools(tool_names: list[str] | tuple[str, ...]) -> dict[str, set[str]]:
    """spec.tool_names 里的 mcp:* 条目 → {server: {线上名}}。"""
    wanted: dict[str, set[str]] = {}
    for name in tool_names:
        parsed = parse_tool_id(name)
        if parsed is not None:
            server, tool = parsed
            wanted.setdefault(server, set()).add(tool)
    return wanted


@dataclass(frozen=True)
class NativeMcpTools:
    catalog: McpCatalog
    #: 全局默认；McpServerConfig.call_timeout_s 可按 server 覆盖
    call_timeout_s: float = 60
    max_result_chars: int = 100_000

    async def tools_for(
        self,
        tool_names: list[str] | tuple[str, ...],
        ctx: CallContext,
        *,
        digests: Mapping[str, str] | None = None,
        drift_policy: str = "warn",
        reviews: ReviewIndex | None = None,
        notices: list[tuple[EventType, dict[str, Any]]] | None = None,
    ) -> list[Any]:
        """按 spec.tool_names 里的 `mcp:*` 条目构造工具。

        缺 server / 缺工具 / 工具不可用 / 缺凭据 —— 一律抛出，冒泡成 run.failed，
        而不是少装几个工具就默默跑（§13.2）。

        未复核 / 被拒的定义、以及 block 策略下漂移了的定义**跳过并告知**
        （notices → mcp.tool_drift）：那是安全闸门在起作用，不是配置错了。
        digests: agent 保存时记录的 {spec 标识: digest}；没有记录的工具不比对。
        """
        wanted = wanted_tools(tool_names)
        if not wanted:
            return []
        servers = list(wanted)
        snaps = await asyncio.gather(*(self.catalog.snapshot(s) for s in servers))
        out: list[Any] = []
        for server, snap in zip(servers, snaps, strict=True):
            names = self._gate(snap, wanted[server], digests or {}, drift_policy, reviews, notices)
            out.extend(self._build(snap, names, ctx))
        return out

    def _gate(
        self,
        snap: ServerSnapshot,
        names: set[str],
        digests: Mapping[str, str],
        policy: str,
        reviews: ReviewIndex | None,
        notices: list[tuple[EventType, dict[str, Any]]] | None,
    ) -> set[str]:
        config = self.catalog.server(snap.server)
        assert config is not None
        keep: set[str] = set()
        for name in sorted(names):
            tool = snap.tool(name)
            if tool is None or not tool.usable:
                keep.add(name)  # 交给 _build 明确报错
                continue
            ident = tool_id(snap.server, name)
            recorded = digests.get(ident)
            status = review_status(config, tool, reviews)
            drifted = bool(recorded) and recorded != tool.digest
            if status in ("pending_review", "rejected"):
                action = status
            elif drifted:
                action = "blocked" if policy == "block" else "used"
            else:
                keep.add(name)
                continue
            if action == "used":
                keep.add(name)
            logger.warning("MCP 工具 %s：%s（digest %s）", ident, action, tool.digest[:12])
            if notices is not None:
                notices.append(
                    (
                        EventType.MCP_TOOL_DRIFT,
                        {
                            "tool": ident,
                            "server": snap.server,
                            "old_digest": recorded,
                            "new_digest": tool.digest,
                            "action": action,
                        },
                    )
                )
        return keep

    def _build(self, snap: ServerSnapshot, names: set[str], ctx: CallContext) -> list[Any]:
        from langchain_mcp_adapters.tools import convert_mcp_tool_to_langchain_tool

        if not names:
            return []  # 全被闸门拦下：不必解析凭据
        config = self.catalog.server(snap.server)
        assert config is not None  # snapshot() 已校验过
        missing = sorted(n for n in names if snap.tool(n) is None)
        if missing:
            raise MissingTool(f"MCP server {snap.server!r} 上不存在工具：{missing}")
        unusable = {n: snap.tool(n).issues for n in names if not snap.tool(n).usable}  # type: ignore[union-attr]
        if unusable:
            raise MissingTool(f"MCP server {snap.server!r} 的工具不可用：{unusable}")

        timeout_s = config.call_timeout_s or self.call_timeout_s
        connection = connection_for(config, timeout_s=timeout_s)
        chain = [
            identity_headers(ctx),
            errors_to_result(snap.server, timeout_s),
            call_timeout(timeout_s),
            clip_result(self.max_result_chars),
        ]
        out = []
        for name in sorted(names):
            d = snap.tool(name)
            assert d is not None and d.model_name is not None
            tool = convert_mcp_tool_to_langchain_tool(
                None,
                d.to_mcp_tool(),
                connection=connection,  # type: ignore[arg-type]
                server_name=snap.server,
                tool_interceptors=chain,
                handle_tool_errors=True,
            )
            # ★ 改的是 LangChain 侧的名字；线上名在 adapter 的闭包里，不受影响
            tool.name = d.model_name
            out.append(tool)
        return out
