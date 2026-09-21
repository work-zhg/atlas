"""五个方法的请求与响应模型。

方法面见 wire.Method。本模块只管参数与结果的形状；谁在什么时候调用
写在 acp 详设 §08–§10 的三张时序图里。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from atlas_acp.caps import AgentCaps

__all__ = [
    "STOP_REASONS",
    "AcpUsage",
    "InitializeParams",
    "InitializeResult",
    "McpServerSpec",
    "PermissionOption",
    "RequestPermissionParams",
    "RequestPermissionResult",
    "SessionCancelParams",
    "SessionLoadParams",
    "SessionNewParams",
    "SessionNewResult",
    "SessionPromptParams",
    "SessionPromptResult",
    "StopReason",
]

_CFG = ConfigDict(populate_by_name=True, extra="allow")

#: 一轮结束的原因。★ 撞上限（max_*）不是失败 —— translate 把它映射成
#: run.finished 加一个 stop_reason 附加字段，而不是 run.failed
#: （acp 详设 §06；也正是 subagent 设计里欠的那条改进）。
StopReason = Literal["end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"]
STOP_REASONS: frozenset[str] = frozenset(
    {"end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"}
)


# ── initialize ──────────────────────────────────────────────────────


class InitializeParams(BaseModel):
    model_config = _CFG

    protocol_version: int = Field(default=1, alias="protocolVersion")
    #: server 侧能力（Atlas 全关：文件与终端都不开反向通道，见 caps.FsCaps）
    client_capabilities: dict[str, Any] = Field(default_factory=dict, alias="clientCapabilities")


class InitializeResult(BaseModel):
    model_config = _CFG

    protocol_version: int = Field(default=1, alias="protocolVersion")
    agent_capabilities: AgentCaps = Field(default_factory=AgentCaps, alias="agentCapabilities")


# ── session/new · session/load ──────────────────────────────────────


class McpServerSpec(BaseModel):
    """随会话下发的 MCP server。

    ★ 随参传入，**不落盘**：落盘的配置文件在 Pod 重建、镜像升级、卷复用时
      产生难以复现的差异（执行环境 §07）。每次建会话显式传，行为完全确定。
    """

    model_config = _CFG

    name: str
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)


class SessionNewParams(BaseModel):
    model_config = _CFG

    #: 恒为 /workspace —— 它就是会话 OSS 前缀的挂载点（文件系统 §05）。
    cwd: str = "/workspace"
    mcp_servers: list[McpServerSpec] = Field(default_factory=list, alias="mcpServers")


class SessionNewResult(BaseModel):
    model_config = _CFG

    #: CLI 侧的会话标识 —— 落 thread.external_session_id，下次 load 用它。
    session_id: str = Field(alias="sessionId")


class SessionLoadParams(BaseModel):
    """恢复一个已有会话。

    ★ 仅当 AgentCaps.can_resume 为真时才发 —— 否则拿到的是
      CAPABILITY_UNSUPPORTED，而调用方本可以提前走 lost 降级。
    """

    model_config = _CFG

    session_id: str = Field(alias="sessionId")
    cwd: str = "/workspace"
    mcp_servers: list[McpServerSpec] = Field(default_factory=list, alias="mcpServers")


# ── session/prompt · session/cancel ─────────────────────────────────


class AcpUsage(BaseModel):
    """CLI 上报的用量。

    ★ 字段与平台 usage.updated 的 data 对齐（translator.normalize_usage 的
      输出形态），这样 acp 与 native 的账走同一套 —— 前端不需要分支。
    """

    model_config = _CFG

    input_tokens: int = Field(default=0, alias="inputTokens")
    output_tokens: int = Field(default=0, alias="outputTokens")
    total_tokens: int = Field(default=0, alias="totalTokens")
    cache_read: int = Field(default=0, alias="cacheRead")
    cache_creation: int = Field(default=0, alias="cacheCreation")
    thinking_tokens: int = Field(default=0, alias="thinkingTokens")


class SessionPromptParams(BaseModel):
    model_config = _CFG

    session_id: str = Field(alias="sessionId")
    #: 本轮的输入。Atlas 只发文本块 —— 会话历史归 CLI 自己维护（驱动模式）。
    prompt: list[dict[str, Any]] = Field(default_factory=list)


class SessionPromptResult(BaseModel):
    """一轮的终了。translate_stop 把它映射成终止事件。"""

    model_config = _CFG

    stop_reason: str = Field(default="end_turn", alias="stopReason")
    usage: AcpUsage | None = None


class SessionCancelParams(BaseModel):
    """notification，不等响应 —— 取消的确认以 prompt 响应的
    stopReason=cancelled 为准。"""

    model_config = _CFG

    session_id: str = Field(alias="sessionId")


# ── session/request_permission（唯一反向请求）────────────────────────


class PermissionOption(BaseModel):
    model_config = _CFG

    option_id: str = Field(default="", alias="optionId")
    name: str = ""
    #: allow_once / allow_always / reject_once / reject_always
    kind: str = ""


class RequestPermissionParams(BaseModel):
    """CLI 要动危险东西时的审批请求。

    ★ Bridge 必须**原样转交**，默认不是自动 allow —— 写死自动批准等于把
      文件写入与命令执行的权限交给模型，前端那套审批卡片成摆设
      （执行环境 §02 的红线）。
    """

    model_config = _CFG

    session_id: str = Field(default="", alias="sessionId")
    tool_call: dict[str, Any] = Field(default_factory=dict, alias="toolCall")
    options: list[PermissionOption] = Field(default_factory=list)


class RequestPermissionResult(BaseModel):
    """用户的决定。

    ★ v1 只映射 allow_once / reject_once。**不透传 allow_always** ——
      「永远允许」是全局策略，必须过权限管理那一篇，不能从一次弹窗里
      溜进来（acp 详设 §07）。
    """

    model_config = _CFG

    outcome: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def selected(cls, option_id: str) -> RequestPermissionResult:
        return cls(outcome={"outcome": "selected", "optionId": option_id})

    @classmethod
    def cancelled(cls) -> RequestPermissionResult:
        return cls(outcome={"outcome": "cancelled"})
