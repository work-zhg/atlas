"""native agent 的 MCP 工具装配（MCP 详设 §06）。

装配期：从目录缓存构造 BaseTool，零网络。缺什么明确失败（§13.2）。
MCP 客户端是 langchain.mcp（beta）+ FastMCP client：每个 run × server 一个 client，
工具由 as_langchain_tool(定义, client) 造出，每次调用 `async with client` → tools/call。

langchain.mcp 没有拦截器，调用期的逻辑分两处：

  ① 传输层        身份请求头（X-Run-Id / X-Thread-Id / X-Uid / X-Auth-Uid，审计用）；
                  经 MCP 网关时还有主体 Key（session.KeyAuth）—— 模型看不到也改不了
  ② 包执行函数    本 Run 段记住的「用不了」→ 按参数定义校验参数 → 持有会话（Run 段内复用）
                  → 超时 → 失败转成给模型的工具结果（能修复的自动重试一次）→ 超大文本结果掐头去尾

全部经 MCP 网关：装配时先问网关「这个智能体的 Key 能用哪些服务和工具、Key 是什么」，
没被授权的服务、工具和管理员停用的工具跳过并提示（模型看不见就不会去调）。网关的拒绝带原因
（error.data.reason），据此给模型不同的说明。

engine 不认识 MCP：这里产出的就是普通 BaseTool，名字已是模型侧名
（server__tool），审批中间件按名字匹配自然生效。
"""

from __future__ import annotations

import asyncio
import logging
import warnings
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from langchain_core.tools import ToolException

from ...domain.events import EventType
from ...domain.mcp_naming import parse_tool_id, tool_id
from ...errors import CapabilityUnavailable
from .arguments import argument_problems
from .catalog import McpCatalog, ServerSnapshot, describe_error, root_causes
from .config import client_for
from .gateway import AgentAccess
from .review import ReviewIndex, review_status
from .session import McpSessions

logger = logging.getLogger(__name__)

ToolCall = Callable[..., Awaitable[Any]]

_RETRY_HINT = "可稍后重试，或换一种方式完成任务。"


class MissingTool(CapabilityUnavailable):
    """spec 里勾了某个 MCP 工具，但目标 server 上没有它（或它不可用）。"""


@dataclass(frozen=True)
class CallContext:
    """闭包捕获，不进工具参数 schema —— 与记忆工具的关联主键同一条纪律：
    一旦身份可由模型指定，就是一个冒用他人身份的口子。"""

    run_id: UUID
    thread_id: UUID
    user_id: UUID
    #: 按谁的权限判（X-Auth-Uid）：以 Agent 为权限时是数字员工。None = 同 user_id
    auth_uid: UUID | None = None


# ---------------------------------------------------------------- 调用期

#: 改名前的请求头（名字里带应用名，数字员工设计 §7.4）。过渡一个版本新旧都发，下个版本删
LEGACY_IDENTITY_HEADERS = {"X-Run-Id": "X-Atlas-Run", "X-Thread-Id": "X-Atlas-Thread"}


def subject_label(ctx: CallContext) -> str:
    """给模型 / 用户看的「MCP 按谁的权限」：智能体的 Key（与智能体的权限类型无关）。"""
    return "这个智能体"


def identity_headers(ctx: CallContext) -> dict[str, str]:
    """与内网同一套身份头（X-Uid / X-Auth-Uid），外部 MCP 也能知道这一轮按谁的权限。

    ★ 名称不带应用名：应用改名时外部对接方不用跟着改。
    """
    headers = {
        "X-Run-Id": str(ctx.run_id),
        "X-Thread-Id": str(ctx.thread_id),
        "X-Uid": str(ctx.user_id),
        "X-Auth-Uid": str(ctx.auth_uid or ctx.user_id),
    }
    headers |= {old: headers[new] for new, old in LEGACY_IDENTITY_HEADERS.items()}
    headers["X-Atlas-User"] = headers["X-Uid"]
    return headers


class McpCallFailed(ToolException):
    """调用失败但不该让整轮失败（§6.7）：转成给模型的工具结果，说明能不能重试、该找谁。"""


@dataclass
class ResponseWatch:
    """这个 client 的 HTTP 层发生过什么（每次调用前清空）。

    ★ mcp 2.x 把 HTTP 4xx 换成一个不带状态码的 MCPError（"Server returned an error response"），
      状态码只能在 HTTP 层记下：挂成 httpx2 的响应钩子。
      · 401 / 403 且没有网关的拒绝原因：网关前面另有一层拒绝了认证（网关自己的拒绝带原因）
      · 带着会话标识的请求被回 404：会话失效（协议规定的写法，§6.3）
    """

    auth_status: int | None = None
    session_lost: bool = False

    async def record(self, response: Any) -> None:
        if response.status_code in (401, 403):
            self.auth_status = response.status_code
        elif response.status_code == 404 and "mcp-session-id" in response.request.headers:
            self.session_lost = True

    def clear(self) -> None:
        self.auth_status, self.session_lost = None, False


#: 网关的拒绝原因（error.data.reason，atlas_mcpgateway/proxy.py::Reason）→ 给模型的说明
#: 与处理方式。retry：能先修复再自动重试一次的；remember：本 Run 段内记住、不再发请求的范围
@dataclass(frozen=True)
class _Failure:
    message: str
    retry: str | None = None  # key | session
    remember: str | None = None  # run | server | tool


_NO_RETRY = "不要重试。"


def _gateway_failure(reason: str, *, server: str, tool: str, subject: str, detail: str) -> _Failure:
    """detail：网关给的说明（error.message）。"""
    match reason:
        case "key_missing" | "key_invalid":
            # ★ 一个 Run 只有一把 Key：取不到新的，这一段里所有 MCP 服务都一样用不了
            return _Failure(
                f"{subject}的 MCP API Key 已失效或已停用，请告知用户联系 MCP 管理员。{_NO_RETRY}",
                retry="key",
                remember="run",
            )
        case "forbidden":
            return _Failure(
                f"{subject}没有被授权使用 MCP 服务 {server}，请告知用户联系该服务的所有者或"
                f" MCP 管理员。{_NO_RETRY}",
                remember="server",
            )
        case "service_disabled" | "service_not_found":
            return _Failure(
                f"MCP 服务 {server} 不存在或已被停用，请告知用户联系 MCP 管理员。{_NO_RETRY}",
                remember="server",
            )
        case "tool_unknown" | "invalid_params":
            return _Failure(
                f"工具 {server}__{tool} 暂时不可用（{detail}）。{_NO_RETRY}", remember="tool"
            )
        case "authz_unavailable":
            return _Failure(f"MCP 服务 {server} 暂时不可用（{detail}）。可以稍后重试。")
        case "upstream_error":
            return _Failure(f"MCP 服务 {server} 返回了错误（{detail}）。{_RETRY_HINT}")
    # upstream_unavailable，以及以后网关新增的原因：按「暂时不可用」交给模型，不让整轮失败
    return _Failure(f"MCP 服务 {server} 暂时不可用（{detail}）。{_RETRY_HINT}")


def _gateway_reason(roots: list[BaseException]) -> tuple[str, str] | None:
    """网关的拒绝：JSON-RPC 错误体里的 data.reason（网关回的；上游服务的错误没有它）。"""
    from mcp.shared.exceptions import MCPError

    for e in roots:
        if isinstance(e, MCPError) and isinstance(e.error.data, dict):
            reason = e.error.data.get("reason")
            if isinstance(reason, str):
                return reason, e.error.message
    return None


def _classify(
    exc: BaseException,
    *,
    server: str,
    tool: str,
    subject: str,
    timeout_s: float,
    read_only: bool,
    watch: ResponseWatch,
) -> _Failure | None:
    """属于传输层 / 网关的异常 → 给模型的说明；其余返回 None（照旧冒泡）。

    ★ 不能写成 except Exception：吞掉 CancelledError 会让取消失效；吞掉编程
      错误会让 bug 伪装成「服务不可用」被模型绕过去，永远不会被发现。
    """
    import httpx2
    from mcp.shared.exceptions import MCPError

    roots = root_causes(exc)
    if any(isinstance(e, asyncio.CancelledError) for e in roots):
        return None
    if (found := _gateway_reason(roots)) is not None:
        return _gateway_failure(
            found[0], server=server, tool=tool, subject=subject, detail=found[1]
        )
    has_mcp_error = any(isinstance(e, MCPError) for e in roots)
    if watch.session_lost and has_mcp_error:
        return _Failure(f"MCP 服务 {server} 的会话已失效。{_RETRY_HINT}", retry="session")
    if watch.auth_status is not None and has_mcp_error:
        return _Failure(
            f"MCP 服务 {server} 拒绝了认证（HTTP {watch.auth_status}），"
            f"请告知用户联系管理员检查该服务的凭据。{_NO_RETRY}"
        )
    if all(isinstance(e, TimeoutError) for e in roots):
        # ★ 超时 = 结果未知（§6.7 ⑫）：有副作用的工具可能已经执行，重试前要先确认
        if read_only:
            return _Failure(f"MCP 服务 {server} 调用 {tool} 超时（{timeout_s:g}s），可以稍后重试。")
        return _Failure(
            f"MCP 服务 {server} 调用 {tool} 超时（{timeout_s:g}s），结果未知：可能已经执行，"
            "重试前先确认（例如先查询一下）。"
        )
    if all(isinstance(e, (httpx2.HTTPError, MCPError, TimeoutError, OSError)) for e in roots):
        return _Failure(f"MCP 服务 {server} 暂时不可用（{describe_error(exc)}）。{_RETRY_HINT}")
    return None


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head, tail = int(limit * 0.7), int(limit * 0.3)
    omitted = len(text) - head - tail
    return f"{text[:head]}\n\n…[结果过长，省略中间 {omitted} 字符]…\n\n{text[-tail:]}"


def _clip_blocks(blocks: Any, limit: int) -> Any:
    """没开文件工具的 agent 没有 FilesystemMiddleware 的转存保护，一个返回
    5 MB JSON 的调用会直接撑爆上下文。这是最后一道。

    ★ 阈值必须高于 FilesystemMiddleware 的转存阈值（20000 token × 4 = 80 000
      字符）：这里在工具内部执行、早于中间件看到结果，阈值更低就会
      抢先裁掉本可完整转存到工作区的内容。
    """
    if not isinstance(blocks, list):
        return blocks
    return [
        {**b, "text": _clip(b["text"], limit)}
        if b.get("type") == "text" and len(b.get("text", "")) > limit
        else b
        for b in blocks
    ]


@dataclass(frozen=True)
class _CallSite:
    """一个工具的调用参数（闭包捕获，不进工具参数）。"""

    server: str
    tool: str
    #: 这一轮的权限主体（给模型的说明里用）
    subject: str
    input_schema: dict[str, Any]
    read_only: bool
    timeout_s: float
    max_chars: int
    watch: ResponseWatch


def _guarded(call: ToolCall, site: _CallSite, sessions: McpSessions) -> ToolCall:
    """包 langchain.mcp 的执行函数（§6.2、§6.7）。

    ★ 自动重试只在确定请求没到后端、原因又能先修复时做，各一次：Key 刚被重置（网关拒绝、
      标明没有转发）、会话失效（协议的「会话不存在」，请求没有被执行）。超时、后端出错一律
      不自动重试：有副作用的工具执行两次，就是两张工单、两封邮件。
    ★ 超时在错误转换里面，否则接不住超时；会话的建立也在超时里面。
    ★ 服务返回的业务错误（isError）在 langchain.mcp 里是 ToolException，原样放过，
      由工具自己的 handle_tool_error 交给模型（见 _error_handler）。
    """

    async def guarded(**arguments: Any) -> Any:
        if known := sessions.blocked(site.server, site.tool):
            raise McpCallFailed(known)
        if problems := argument_problems(site.input_schema, arguments):
            raise McpCallFailed(
                f"参数不合法：{'；'.join(problems)}。请按工具的参数定义改正后再调用。"
            )
        for attempt in (1, 2):
            site.watch.clear()
            try:
                async with asyncio.timeout(site.timeout_s):
                    await sessions.hold(site.server)
                    content, artifact = await call(**arguments)
            except ToolException:
                raise
            except BaseException as exc:
                failure = _classify(
                    exc,
                    server=site.server,
                    tool=site.tool,
                    subject=site.subject,
                    timeout_s=site.timeout_s,
                    read_only=site.read_only,
                    watch=site.watch,
                )
                if failure is None:
                    raise
                if attempt == 1 and await _recover(failure, sessions, site.server):
                    continue
                failure = _settle(failure, sessions, site)
                logger.warning(
                    "MCP 调用失败 server=%s tool=%s：%s",
                    site.server,
                    site.tool,
                    describe_error(exc),
                )
                raise McpCallFailed(failure.message) from exc
            return _clip_blocks(content, site.max_chars), artifact
        raise AssertionError("unreachable")

    return guarded


async def _recover(failure: _Failure, sessions: McpSessions, server: str) -> bool:
    """能先修复的失败（§6.7 ④ ⑧）：修好了 → True（重试一次）。"""
    if failure.retry is None:
        return False
    if failure.retry == "key":
        return await sessions.refresh_key()
    await sessions.reset(server)
    return True


def _settle(failure: _Failure, sessions: McpSessions, site: _CallSite) -> _Failure:
    """最终交给模型之前：② ④ ⑤ ⑥ 的结论在本 Run 段内记住，之后不再发请求（§6.7）。"""
    if failure.remember is not None:
        server = None if failure.remember == "run" else site.server
        sessions.block(server, site.tool if failure.remember == "tool" else None, failure.message)
    return failure


def _error_handler(original: Any, max_chars: int) -> Callable[[ToolException], Any]:
    """工具错误 → ToolMessage(status="error") 的内容。

    ★ langchain.mcp 自带的处理器只认它自己的业务错误，别的 ToolException 一律
      重新抛出 —— McpCallFailed 要在这里先接住。业务错误的内容同样要截断。
    """

    def handle(error: ToolException) -> Any:
        if isinstance(error, McpCallFailed):
            return str(error)
        return _clip_blocks(original(error), max_chars)

    return handle


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
        access: AgentAccess,
        sessions: McpSessions,
    ) -> list[Any]:
        """按 spec.tool_names 里的 `mcp:*` 条目构造工具。

        缺 server / 缺工具 / 工具不可用 / 缺凭据 —— 一律抛出，冒泡成 run.failed，
        而不是少装几个工具就默默跑（§13.2）。

        「此刻用不了」的**跳过并告知**，Run 照常进行：
          · block 策略下漂移了的定义（notices → mcp.tool_drift）
          · 智能体的 Key 没被授权这个服务 / 工具、Key 停用了、管理员停用了工具
            （notices → mcp.tool_skipped）
        digests: agent 保存时记录的 {spec 标识: digest}；没有记录的工具不比对。
        access: 这个智能体的 Key 能用什么（服务、工具、Key），装配前问过网关
        sessions: 本 Run 段的会话（复用、主体 Key、「用不了」的记忆）；段结束由调用方关闭
        """
        wanted = wanted_tools(tool_names)
        if not wanted:
            return []
        servers = list(wanted)
        # ★ 先取定义：服务没注册、已停用是配置错误，明确失败（不当成「没权限」跳过）
        snaps = await asyncio.gather(*(self.catalog.snapshot(s) for s in servers))
        out: list[Any] = []
        for server, snap in zip(servers, snaps, strict=True):
            if not self._admitted(server, wanted[server], ctx, access, notices):
                continue
            names = self._granted(snap, wanted[server], access, notices)
            names = self._gate(snap, names, digests or {}, drift_policy, reviews, notices)
            out.extend(await self._build(snap, names, ctx, sessions))
        return out

    @staticmethod
    def _admitted(
        server: str,
        names: set[str],
        ctx: CallContext,
        access: AgentAccess,
        notices: list[tuple[EventType, dict[str, Any]]] | None,
    ) -> bool:
        """闸门一：Key 停用了、没被授权的服务跳过并提示 —— 模型看不见就不会去调。

        ★ 跳过一定要提示：静默少一个工具，模型照常做事、只是做不好，用户不知道为什么。
        """
        if access.allows(server) and access.key is not None:
            return True
        who = subject_label(ctx)
        if access.key is None:
            reason, message = "no_key", f"{who}的 MCP Key 已停用"
        else:
            reason, message = "forbidden", f"{who}的 MCP Key 没有被授权使用 MCP 服务 {server}"
        logger.warning("跳过 MCP 服务 %s：%s（主体 %s）", server, reason, access.subject)
        if notices is not None:
            notices.append(
                (
                    EventType.MCP_TOOL_SKIPPED,
                    {
                        "server": server,
                        "tools": [tool_id(server, n) for n in sorted(names)],
                        "reason": reason,
                        "subject": access.subject,
                        "message": f"{message}，本轮跳过；请联系服务所有者或 MCP 管理员",
                    },
                )
            )
        return False

    @staticmethod
    def _granted(
        snap: ServerSnapshot,
        names: set[str],
        access: AgentAccess,
        notices: list[tuple[EventType, dict[str, Any]]] | None,
    ) -> set[str]:
        """闸门二：工具级授权、管理员停用。跳过并提示；被删掉的工具留给 _build 明确报错。

        ★ 停用不让整轮失败：它是管理员有意的运维动作，智能体照常跑，只是少了这个工具。
        """
        allowed = access.tools_of(snap.server)
        keep: set[str] = set()
        denied: list[str] = []
        disabled: list[str] = []
        for name in sorted(names):
            tool = snap.tool(name)
            if tool is None:
                keep.add(name)
            elif not tool.usable:
                disabled.append(name)
            elif name not in allowed:
                denied.append(name)
            else:
                keep.add(name)
        for reason, skipped, message in (
            ("forbidden", denied, "这个智能体的 MCP Key 没有被授权使用这些工具"),
            ("unavailable", disabled, "这些工具不可用（管理员停用了，或定义不合规）"),
        ):
            if not skipped:
                continue
            logger.warning("跳过 MCP 工具 %s：%s", [f"{snap.server}__{n}" for n in skipped], reason)
            if notices is not None:
                notices.append(
                    (
                        EventType.MCP_TOOL_SKIPPED,
                        {
                            "server": snap.server,
                            "tools": [tool_id(snap.server, n) for n in skipped],
                            "reason": reason,
                            "subject": access.subject,
                            "message": f"{message}，本轮跳过；请联系服务所有者或 MCP 管理员",
                        },
                    )
                )
        return keep

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

    async def _build(
        self,
        snap: ServerSnapshot,
        names: set[str],
        ctx: CallContext,
        sessions: McpSessions,
    ) -> list[Any]:
        from langchain_core._api import LangChainBetaWarning

        with warnings.catch_warnings():
            # 导入即警告 beta：有意采用（只用最底层的 as_langchain_tool，见 pyproject）
            warnings.simplefilter("ignore", LangChainBetaWarning)
            from langchain.mcp import as_langchain_tool

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
        watch = ResponseWatch()
        # ★ 一个 run × server 一个 client，身份请求头和主体 Key 写在传输层；sessions 持有它，
        #   Run 段内复用一个会话
        client = client_for(
            config, headers=identity_headers(ctx), on_response=watch.record, auth=sessions.auth
        )
        sessions.register(snap.server, client)
        out = []
        for name in sorted(names):
            d = snap.tool(name)
            assert d is not None and d.model_name is not None
            tool = await as_langchain_tool(d.to_mcp_tool(), client)
            assert tool.coroutine is not None
            site = _CallSite(
                server=snap.server,
                tool=name,
                subject=subject_label(ctx),
                input_schema=d.input_schema,
                read_only=(d.annotations or {}).get("readOnlyHint") is True,
                timeout_s=timeout_s,
                max_chars=self.max_result_chars,
                watch=watch,
            )
            tool.coroutine = _guarded(tool.coroutine, site, sessions)
            tool.handle_tool_error = _error_handler(tool.handle_tool_error, self.max_result_chars)
            # ★ 审批按 metadata["readOnlyHint"] 判只读（tool_registry.tool_effects）。
            #   langchain.mcp 把注解收在 metadata["mcp"]["tool"] 下、键是 snake_case，
            #   这里按线上名平铺回顶层；值取目录里那份（复核、指纹看的也是它）
            tool.metadata = {**(tool.metadata or {}), **(d.annotations or {})}
            # ★ 改的是 LangChain 侧的名字；线上名在 langchain.mcp 的闭包里，不受影响
            tool.name = d.model_name
            out.append(tool)
        return out
