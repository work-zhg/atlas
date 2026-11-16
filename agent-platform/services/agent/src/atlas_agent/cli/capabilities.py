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

from atlas_engine.contracts.events import EventType
from atlas_engine.contracts.mcp_naming import MCP_PREFIX
from atlas_engine.contracts.tool_names import REQUEST_TAKEOVER
from atlas_engine.runtime import TAKEOVER_DESCRIPTION, TakeoverAsk

from ..browser.plan import BrowserPlan, browser_hint
from ..credentials import (
    CredentialDenied,
    CredentialGrants,
    CredentialProfileStore,
    CredentialSet,
    CredentialSubject,
)
from ..errors import CapabilityUnavailable
from ..providers.filesystem import make_workspace
from ..providers.filesystem.skill_copy import resolve_skills, seed_session_skills
from ..providers.knowledge import (
    MCP_LIST_TOOL,
    MCP_SEARCH_TOOL,
    mcp_server_entry,
    require_client,
    resolve_knowledge,
)
from ..providers.llm.model_platform import (
    admit_models,
    agent_key,
    litellm_url,
    sees_images,
    uses_model_platform,
)
from ..providers.mcp import GatewayUnavailable, make_mcp_catalog, mcp_agent_access
from ..providers.mcp.cli import cli_mcp_servers
from ..providers.mcp.gateway import servers_in
from ..repositories.model_catalog import ModelCatalogRepository
from ..runtime_config import RuntimeConfig
from ..sandbox.session_pod import CliModel
from ..skills import skill_directory

if TYPE_CHECKING:
    import redis.asyncio as aioredis
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
    #: 追加在 CLI 系统提示词后面的知识集目录（知识集方案 §3.4）；None = 没有能查的知识集
    system_prompt_append: str | None = None
    #: 解析时要告诉用户的（比如 MCP 网关不可用，这些服务本会话不下发）
    notices: list[tuple[EventType, dict[str, Any]]] = field(default_factory=list)
    #: 经平台时的模型接入；None = 照旧按部署配置
    model: CliModel | None = None
    #: 这一轮授权过的出站凭据（会话 Pod 经 Credential Vault 注入）；空 = 会话没选凭据档案
    credentials: CredentialSet = field(default_factory=CredentialSet)
    #: 一轮的时限：只下发静默检测的两个阈值（唯一的「卡死」判据）。截止、审批等待、
    #: 重连窗口不下发 = 不限：CLI 在干活就不中断，审批只由人决定
    limits: dict[str, float] = field(default_factory=dict)
    #: 这一轮按什么权限模式要求 CLI（平台取值，ModeMapping.platform_mode）。本机 CLI 开会话时交给
    #: 电脑上的连接器，按本机策略的上限核对；None = 没定
    permission_mode: str | None = None
    #: 浏览器（c4-model/agent/browser/design-browser.html）：开了就换浏览器 CLI 镜像；None = 没开
    browser: BrowserPlan | None = None
    #: 平台工具（atlas_host.PlatformTool 的线上形态）：bridge 起本机 MCP 提供给 CLI，调用经
    #: tool.call 回到平台。开了浏览器才有 request_takeover
    platform_tools: list[dict[str, Any]] = field(default_factory=list)


class CliCapabilityAssembly:
    def __init__(self, settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._settings = settings
        self._sessionmaker = sessionmaker
        self._grants = CredentialGrants(CredentialProfileStore.from_settings(settings))

    async def resolve(self, segment: Segment, redis: aioredis.Redis) -> CliCapabilities:
        """解析本轮的能力。不可用抛 CapabilityUnavailable（调用方报 run.failed）。"""
        servers, notices, catalog = await self._mcp_servers(segment, redis)
        browser = await self._browser(segment)
        prompt = catalog
        tools: list[dict[str, Any]] = []
        if browser is not None:
            # 浏览器：告诉 CLI 这次能用哪几种操作方式；请人接管是平台工具（不设期限）
            vision = await sees_images(self._settings, segment.spec.model.model)
            hint = browser_hint(browser, vision=vision)
            prompt = f"{catalog}\n\n{hint}" if catalog else hint
            tools = [REQUEST_TAKEOVER_TOOL]
        return CliCapabilities(
            mcp_servers=servers,
            system_prompt_append=prompt,
            platform_tools=tools,
            notices=notices,
            model=await self._model(segment),
            credentials=self._credentials(segment),
            limits={
                "idleS": self._settings.cli_prompt_idle_timeout_s,
                "toolIdleS": self._settings.cli_tool_idle_timeout_s,
            },
            browser=browser,
        )

    async def _browser(self, segment: Segment) -> BrowserPlan | None:
        """开了浏览器：换算浏览器配置（每轮读运行期配置），把浏览器技能拷进这个会话。

        部署没有浏览器 CLI 镜像时抛 BrowserNotConfigured：本轮不开始，告诉用户。
        """
        spec = segment.spec
        if not spec.browser:
            return None
        config = await RuntimeConfig(self._settings, self._sessionmaker).browser_config()
        plan = BrowserPlan.of(spec, config)
        if plan is not None and plan.runtime != "device":
            # 本机 CLI 的技能随浏览器组件装在电脑上；会话 Pod 里的拷进会话
            vision = await sees_images(self._settings, spec.model.model)
            skills = list(plan.skills((), vision=vision))
            if skills:
                await self._seed_skills(segment, skills)
        return plan

    async def _seed_skills(self, segment: Segment, skills: list[Any]) -> None:
        """浏览器技能不在智能体配的技能里（随开关自动带上）：建会话时没拷，每轮确认一次。

        只是 HEAD：拷过的不再拷。
        """
        thread = segment.thread
        fs = make_workspace(
            self._settings, thread.created_by_uuid, thread.thread_uuid, thread.workspace_dir
        )
        if fs is None:
            return
        try:
            metas, _ = await resolve_skills(
                skills, skill_directory(self._settings), with_descriptions=False
            )
            await seed_session_skills(fs, metas)
        except Exception:
            # 技能拷不进去不挡住这一轮：浏览器照样能用，模型少一份说明
            logger.warning("会话 %s 拷浏览器技能失败", thread.thread_uuid, exc_info=True)

    async def _mcp_servers(
        self, segment: Segment, redis: aioredis.Redis
    ) -> tuple[list[dict[str, Any]], list[tuple[EventType, dict[str, Any]]], str | None]:
        """→ (MCP 连接配置, 要告诉用户的, 知识集目录)。"""
        spec = segment.spec
        servers: list[dict[str, Any]] = []
        notices: list[tuple[EventType, dict[str, Any]]] = []
        if any(name.startswith(MCP_PREFIX) for name in spec.tool_names):
            servers, notices = await self._mcp_from_gateway(segment, redis)
        # 知识集（知识集方案 §3.4）：Pod 里的 CLI 经知识库的 MCP 调 knowledge_list /
        # knowledge_search；目录追加进它的系统提示词。令牌里签的是这一刻展开好的知识集
        resolution = await resolve_knowledge(self._settings, spec.knowledge)
        notices.extend(resolution.notices())
        if resolution.turn is None:
            return servers, notices, None
        servers = [
            *servers,
            mcp_server_entry(
                require_client(self._settings), resolution.turn, thread_id=segment.thread_id
            ),
        ]
        catalog = resolution.turn.catalog(list_tool=MCP_LIST_TOOL, search_tool=MCP_SEARCH_TOOL)
        return servers, notices, catalog

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
        ★ 提示词缓存与原生同一条规则（executor/assembly.py 的 _prompt_cache_ttl）：智能体开了、
          且模型实测支持才让 CLI 发 cache_control。
        """
        spec = segment.spec
        if not uses_model_platform(self._settings) or spec.cli is None:
            return None
        if spec.cli.model_source == "local_login":
            return None  # 本机 CLI 用电脑上自己的登录：不取 Key、不查模型分配
        if spec.cli.cli_type != "claude-code":
            return None
        key = await agent_key(self._settings, segment.thread.agent_uuid, spec.name)
        admit_models(key, [spec.model.model])
        assert key.key is not None
        prompt_cache = spec.model.prompt_cache
        if prompt_cache:
            async with self._sessionmaker() as session:
                prompt_cache = await ModelCatalogRepository(session).supports_cache(
                    spec.model.model
                )
        return CliModel(
            base_url=litellm_url(self._settings),
            key=key.key,
            model=spec.model.model,
            small_model=spec.model.model,
            headers={},
            prompt_cache=prompt_cache,
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


#: 请人接管浏览器（平台工具）：描述和参数和原生的 request_takeover 是同一份
REQUEST_TAKEOVER_TOOL: dict[str, Any] = {
    "name": REQUEST_TAKEOVER,
    "description": TAKEOVER_DESCRIPTION,
    "inputSchema": TakeoverAsk.model_json_schema(),
}
