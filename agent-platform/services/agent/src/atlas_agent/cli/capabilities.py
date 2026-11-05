"""本轮交给 CLI 的能力（code-cli-runtime-turn.html · CliCapabilityAssembly）。

MCP 服务的连接配置（经 MCP 网关）、模型接入（平台的模型网关地址和智能体的客户端 Key）、这一轮
授权过的出站凭据（凭据组件，每轮重新授权：收回权限、换了令牌下一轮就生效）、时限。
任何一项不可用就在开始前报 run.failed，并说明是哪一项 —— CLI 跑起来也会在中途失败，而且错误
藏在 CLI 的输出里。

★ MCP 和模型在建会话（session.open）时一次性下发，整个会话沿用：CLI 进程启动时读取这些配置，
  中途换不了。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..credentials import (
    CredentialDenied,
    CredentialGrants,
    CredentialProfileStore,
    CredentialSet,
    CredentialSubject,
)
from ..domain.events import EventType
from ..domain.mcp_naming import MCP_PREFIX
from ..errors import CapabilityUnavailable
from ..providers.knowledge import mcp_server_entry, require_client
from ..providers.llm.model_platform import admit_models, agent_key, litellm_url, uses_model_platform
from ..providers.mcp import GatewayUnavailable, make_mcp_catalog, mcp_agent_access
from ..providers.mcp.cli import cli_mcp_servers
from ..providers.mcp.gateway import servers_in
from ..sandbox.session_pod import CliModel

if TYPE_CHECKING:
    import redis.asyncio as aioredis

    from ..config import Settings
    from ..executor.segment_data import Segment

__all__ = ["CliCapabilities", "CliCapabilityAssembly", "CredentialsUnavailable"]

logger = logging.getLogger(__name__)


class CredentialsUnavailable(CapabilityUnavailable):
    """会话选的凭据档案这一轮用不了（不存在，或权限被收回）。"""


@dataclass
class CliCapabilities:
    """本轮交给 CLI 的全部能力。"""

    #: MCP 服务的连接配置（网关上的服务 + 知识库 MCP）
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    #: 解析时要告诉用户的（比如 MCP 网关不可用，这些服务本会话不下发）
    notices: list[tuple[EventType, dict[str, Any]]] = field(default_factory=list)
    #: 经平台时的模型接入；None = 照旧按部署配置
    model: CliModel | None = None
    #: 这一轮授权过的出站凭据（会话 Pod 经 Credential Vault 注入）；空 = 会话没选凭据档案
    credentials: CredentialSet = field(default_factory=CredentialSet)
    #: 一轮的时限：只下发静默检测的两个阈值（唯一的「卡死」判据）。截止、审批等待、
    #: 重连窗口不下发 = 不限：CLI 在干活就不中断，审批只由人决定
    limits: dict[str, float] = field(default_factory=dict)


class CliCapabilityAssembly:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._grants = CredentialGrants(CredentialProfileStore.from_settings(settings))

    async def resolve(self, segment: Segment, redis: aioredis.Redis) -> CliCapabilities:
        """解析本轮的能力。不可用抛 CapabilityUnavailable（调用方报 run.failed）。"""
        servers, notices = await self._mcp_servers(segment, redis)
        return CliCapabilities(
            mcp_servers=servers,
            notices=notices,
            model=await self._model(segment),
            credentials=self._credentials(segment),
            limits={
                "idleS": self._settings.cli_prompt_idle_timeout_s,
                "toolIdleS": self._settings.cli_tool_idle_timeout_s,
            },
        )

    async def _mcp_servers(
        self, segment: Segment, redis: aioredis.Redis
    ) -> tuple[list[dict[str, Any]], list[tuple[EventType, dict[str, Any]]]]:
        spec = segment.spec
        servers: list[dict[str, Any]] = []
        notices: list[tuple[EventType, dict[str, Any]]] = []
        if any(name.startswith(MCP_PREFIX) for name in spec.tool_names):
            servers, notices = await self._mcp_from_gateway(segment, redis)
        # 知识库：Pod 里的 CLI 经 MCP 调 knowledge_search（doc/atlas-rag-design.html §10）
        if spec.knowledge.enabled:
            servers = [
                *servers,
                mcp_server_entry(
                    require_client(self._settings),
                    spec.knowledge,
                    thread_id=segment.thread_id,
                ),
            ]
        return servers, notices

    def _credentials(self, segment: Segment) -> CredentialSet:
        """每轮按当前的档案和规则重新授权（c4-model/agent/code-credentials.html）。

        档案不存在或权限被收回：这一轮不开始（run.failed 说明是哪份档案）。
        """
        try:
            return self._grants.resolve(CredentialSubject.of(segment.thread))
        except CredentialDenied as exc:
            raise CredentialsUnavailable(str(exc)) from exc

    async def _model(self, segment: Segment) -> CliModel | None:
        """经模型管理平台时 Pod 里 CLI 的模型接入（模型管理方案设计 8.1）。

        没接平台、不是 Claude Code → None（照旧按部署配置）。Key 待分配、停用或模型没分配
        抛 ModelNotAllowed：本轮不开始。

        下发 LiteLLM 地址 + 智能体的客户端 Key + 模型名；Pod 里不再有供应商凭据。
        ★ Claude Code 只会 Anthropic Messages：LiteLLM 负责转换，任何已分配的模型都能用。
        ★ CLI 的轻量模型也指向同一个模型：不指的话 CLI 会去找一个它默认的、Key 里未必有的模型。
        """
        spec = segment.spec
        if not uses_model_platform(self._settings) or spec.cli is None:
            return None
        if spec.cli.cli_type != "claude-code":
            return None
        key = await agent_key(self._settings, segment.thread.agent_uuid, spec.name)
        admit_models(key, [spec.model.model])
        assert key.key is not None
        return CliModel(
            base_url=litellm_url(self._settings),
            key=key.key,
            model=spec.model.model,
            small_model=spec.model.model,
            headers={},
        )

    async def _mcp_from_gateway(
        self, segment: Segment, redis: aioredis.Redis
    ) -> tuple[list[dict[str, Any]], list[tuple[EventType, dict[str, Any]]]]:
        spec = segment.spec
        catalog = await make_mcp_catalog(self._settings, redis)
        # 按这个智能体的 Key 过滤、下发它的 Key（与权限类型无关）
        agent_id = segment.thread.agent_uuid
        try:
            access = await mcp_agent_access(
                self._settings, agent_id, name=spec.name, owner=segment.thread.created_by_uuid
            )
        except GatewayUnavailable as exc:
            # 网关不可用 = 这些服务此刻用不了：不下发并提示，会话照常（§6.7 ③）
            logger.warning("建会话时 MCP 网关不可用，本会话不下发 MCP 服务：%s", exc)
            return [], [
                (
                    EventType.MCP_TOOL_SKIPPED,
                    {
                        "server": server,
                        "tools": [f"mcp:{server}:*"],
                        "reason": "unavailable",
                        "subject": str(agent_id),
                        "message": f"{exc}，本会话不下发这个服务",
                    },
                )
                for server in sorted(servers_in(spec.tool_names))
            ]
        return await cli_mcp_servers(
            spec.tool_names,
            catalog,
            user_id=segment.thread.created_by_uuid,
            digests=spec.mcp_tool_digests,
            drift_policy=spec.mcp_drift_policy,
            access=access,
        )
