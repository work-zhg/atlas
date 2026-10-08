"""上游协议各方法的参数与结果模型（Bridge 设计 §4 · §5.6 · §7.3）。

所有 bridge → server 的消息都带 ``seq``：一个会话内从 1 开始严格递增、不跳号、跨连接连续。
ACP 的内容（update、权限请求、MCP 配置、prompt 内容块）一律是 ``JsonObject``：
上游协议运送它们，但不理解它们（§3 D2；本包不得导入 atlas_acp）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import Field, model_validator

from ._model import JsonObject, Model
from .limits import TurnLimits
from .outcomes import ErrorInfo, Outcome, SessionState, TurnState

__all__ = [
    "AckParams",
    "AcpNegotiation",
    "AgentRestart",
    "AgentUpdateParams",
    "AttachParams",
    "AttachResult",
    "BridgeInfo",
    "CloseParams",
    "CloseResult",
    "ModeState",
    "OpenParams",
    "OpenResult",
    "PermissionAnswer",
    "PermissionAskParams",
    "PermissionWithdrawParams",
    "ResumeSpec",
    "SeqRange",
    "SessionEndedParams",
    "SessionStateParams",
    "TurnCancelParams",
    "TurnCancelResult",
    "TurnRef",
    "TurnStartParams",
    "TurnStartResult",
    "TurnStateParams",
    "TurnStats",
]

Seq = Field(ge=1)
ReplayWant = Literal["none", "full"]

# ═════════════════════════════════ session.open ═════════════════════════════════


class ResumeSpec(Model):
    agent_session_id: str = Field(min_length=1)
    #: server 要不要历史：none = 只要恢复上下文；full = 还要把历史重放一遍
    replay: ReplayWant = "none"


class OpenParams(Model):
    #: 省略 = 新建会话
    resume: ResumeSpec | None = None
    #: ACP 格式的 MCP server 配置，bridge 原样放进 session/new 等请求
    mcp_servers: list[JsonObject] = Field(default_factory=list)
    #: 整个会话的时限默认值，每一轮可在 turn.start 里覆盖
    defaults: TurnLimits = Field(default_factory=TurnLimits)


class AcpNegotiation(Model):
    """ACP 握手的结果。server 的翻译层据此选择规则（§3.7）。内容对上游协议是不透明的。"""

    protocol_version: int
    agent_info: JsonObject | None = None
    agent_capabilities: JsonObject = Field(default_factory=dict)


class BridgeInfo(Model):
    version: str
    #: bridge 进程的实例 id，每次启动随机生成。server 据此判断 bridge 是否重启过（§7.6）
    instance: str


class OpenResult(Model):
    agent_session_id: str
    #: 要求恢复却没恢复成时为 False —— 上下文丢失，必须让 server 知道（§5.6）
    resumed: bool
    #: 实际是否重放了历史
    replayed: ReplayWant
    acp: AcpNegotiation
    bridge: BridgeInfo
    #: 兼容性新增的声明（§4.3）
    features: list[str] = Field(default_factory=list)


# ════════════════════════════════ session.attach ════════════════════════════════


class AttachParams(Model):
    #: 上次见到的 bridge 实例 id
    bridge_instance: str
    #: server 已**处理完成**的最后一条（0 = 一条都没有）
    last_seq: int = Field(ge=0)


class TurnRef(Model):
    turn_id: str
    state: TurnState


class SeqRange(Model):
    """闭区间 [first, last]，被淘汰、无法补发的序号（§7.4）。"""

    first: int = Seq
    last: int = Seq

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.last < self.first:
            raise ValueError("last 不能小于 first")
        return self


class AttachResult(Model):
    state: SessionState
    turn: TurnRef | None = None
    #: 随后补发的第一条序号
    resend_from: int = Seq
    gaps: list[SeqRange] = Field(default_factory=list)


# ═══════════════════════════ session.ack / close ════════════════════════════


class AckParams(Model):
    #: seq ≤ 此值的消息已经处理完成（入库），而不只是收到
    seq: int = Field(ge=0)


class CloseParams(Model):
    pass


class CloseResult(Model):
    pass


# ═════════════════════════ session.state / ended ══════════════════════════


class ModeState(Model):
    """agent 的权限模式（ACP session modes）：server 要的、实际生效的、可选的。

    ★ 模式在 agent 每次新建 / 恢复会话时都会被重置，所以 bridge 记住 server 要的那个，
      每一轮开始前、每次恢复会话后都重新应用（doc/acp-permission-mode-design.html）。
    """

    #: server 在 turn.start 里要的 modeId。None = server 没要求（保持 agent 当前的模式）
    requested: str | None = None
    #: agent 实际生效的 modeId。None = agent 不支持模式
    effective: str | None = None
    available: list[str] = Field(default_factory=list)
    #: requested 与 effective 不一致：agent 不支持、设置失败，或 agent 自己降级了
    degraded: bool = False


class AgentRestart(Model):
    restarts: int = Field(ge=0)
    cause: str | None = None
    #: 仅在 state = ready 时有意义：False = 恢复失败，已新建会话，上下文丢失
    resumed: bool | None = None
    #: 仅在 state = ready 时给出：恢复后的 agent 会话 id（新建了会话时与之前不同）
    agent_session_id: str | None = None
    #: 仅在 state = ready 时给出：恢复后重新应用的权限模式
    mode: ModeState | None = None


class SessionStateParams(Model):
    seq: int = Seq
    state: Literal["recovering", "ready"]
    agent: AgentRestart


class SessionEndedParams(Model):
    seq: int = Seq
    cause: str
    error: ErrorInfo | None = None


# ════════════════════════════════ turn.* ════════════════════════════════════


class TurnStartParams(Model):
    #: server 分配（通常就是平台的 run id）。同一个 turnId 重复提交是安全的（§4.6）
    turn_id: str = Field(min_length=1)
    #: ACP 内容块，原样放进 session/prompt
    prompt: list[JsonObject] = Field(min_length=1)
    limits: TurnLimits | None = None
    #: W3C traceparent（§8.4）
    trace: str | None = None
    #: 这一轮要求的权限模式（ACP modeId，如 "auto"）。None = 不要求，保持当前模式
    mode: str | None = None


class TurnStartResult(Model):
    accepted: bool = True


class TurnCancelParams(Model):
    turn_id: str


class TurnCancelResult(Model):
    #: cancelling = 已开始取消；ended = 这一轮早已结束，取消是空操作
    state: Literal["cancelling", "ended"]


class TurnStats(Model):
    elapsed_s: float = Field(ge=0)
    awaiting_permission_s: float = Field(ge=0)


class TurnStateParams(Model):
    seq: int = Seq
    turn_id: str
    state: TurnState
    #: state = ended 时必有，其余状态必无（B1：结束只宣告一次，且带结果）
    outcome: Outcome | None = None
    stats: TurnStats | None = None
    #: 只在这一轮开始（第一个 running）时带：本轮实际生效的权限模式
    mode: ModeState | None = None

    @model_validator(mode="after")
    def _outcome_iff_ended(self) -> Self:
        if (self.state == TurnState.ENDED) != (self.outcome is not None):
            raise ValueError("outcome 必须且只能在 state = ended 时出现")
        return self


# ═════════════════════════════ permission.* ═════════════════════════════════


class PermissionAskParams(Model):
    seq: int = Seq
    turn_id: str
    #: ACP session/request_permission 的 params，原样
    request: JsonObject
    #: 展示用的绝对时刻（前端倒计时）。None = 不过期：一直等到人做决定
    expires_at: datetime | None = None
    #: 判断用：跨进程只传时长（§6.7）。None = 不过期
    expires_in_s: float | None = Field(default=None, ge=0)


class PermissionAnswer(Model):
    """server 的答复：从 agent 给出的选项里选一个，或者拒绝。二者恰好其一。"""

    option_id: str | None = None
    reject: bool | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.option_id is None) == (self.reject is not True):
            raise ValueError("optionId 与 reject: true 必须恰好给出其一")
        return self


class PermissionWithdrawParams(Model):
    seq: int = Seq
    #: 被撤回的那个 permission.ask 的 JSON-RPC id
    ask_id: int
    reason: Literal["turn_ended", "expired", "agent_withdrew"]


# ═════════════════════════════════ agent.update ═════════════════════════════════


class AgentUpdateParams(Model):
    seq: int = Seq
    #: replay = 会话打开时重放的历史；turn = 某一轮期间；stray = 没有进行中的一轮时到达
    origin: Literal["replay", "turn", "stray"]
    turn_id: str | None = None
    #: ACP session/update 的 params，一字不改（§3.8 B2）
    update: JsonObject

    @model_validator(mode="after")
    def _turn_id_iff_turn(self) -> Self:
        if (self.origin == "turn") != (self.turn_id is not None):
            raise ValueError("turnId 必须且只能在 origin = turn 时出现")
        return self
