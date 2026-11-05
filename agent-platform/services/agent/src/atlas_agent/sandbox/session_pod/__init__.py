"""沙箱 · 会话 Pod（c4-model/agent/sandbox/code-sandbox-session-pod.html）。

外部 CLI 智能体的运行环境。

按会话要 Pod：换算成通用供给的业务时，类型是 CLI_AGENT、业务 id 是会话的 uuid、人是会话的创建人；
Pod 归人，由通用供给管理。工作区挂根会话的，技能和 CLI 的 HOME 挂自己的，HOME 持久化（Pod 重建后
CLI 接着上次的会话）。

    PodProvider · PodEndpoint   CLI 运行时拿会话 Pod 的唯一接口：bridge 的 ws 地址和服务令牌
    CliModel · PodRequest       这一轮的模型接入、建 Pod 要的会话信息
    CliPodKind                  会话 Pod 这一种（SandboxKind）：模板 cli；凭据非空时开凭据注入、每轮
                                整份重写；就绪看 bridge /readyz
    SessionPods                 ensure · release · reap_idle · reap_orphans

★ CLI 镜像、模型接入和凭据都走参数：PodProvider 是进程级的，而镜像和模型来自这一轮的 Agent 版本
  快照，凭据由凭据组件每轮授权。会话 Pod 不判断谁能用哪份凭据，也不认识 git。
★ 交出 bridge 服务令牌，不交 execd 令牌：拿到它也不能在 Pod 里执行任意命令。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, NamedTuple, Protocol, runtime_checkable
from uuid import UUID

import httpx
from pydantic import SecretStr

from ...credentials import CredentialSet, GitAuthor
from ...domain.workspace_layout import SKILLS_MOUNT, WORKSPACE_MOUNT, Area, WorkspaceLayout
from ..base.lifecycle.runtime import SandboxRuntime
from ..base.lifecycle.spec import CreateSpec, Labels, Resources, Volume
from ..base.network.egress import EgressControl
from ..base.network.policy import MitmTrust, NetworkPolicy
from ..provisioning.model import BizRef, BizType, SandboxRef
from ..provisioning.service import SandboxProvisioning

if TYPE_CHECKING:
    from ...config import Settings
    from ...db.models import Thread
    from ...domain.spec import CliSpec

__all__ = [
    "CLI_TEMPLATE",
    "CliModel",
    "CliPodKind",
    "PodEndpoint",
    "PodProvider",
    "PodRequest",
    "SessionPods",
]

logger = logging.getLogger(__name__)

CLI_TEMPLATE = "cli"
#: CLI 的 HOME。★ 必须持久化：resume 靠它（CLI 把会话记录写在 HOME 下），而 Pod 会被删掉重建
STATE_MOUNT = "/state"
#: bridge 读令牌的文件（entrypoint 从 env 写出来；/tmp 是模板里的 emptyDir）
TOKEN_FILE = "/tmp/.atlas-bridge-token"
#: bridge 的服务令牌放在这个环境变量里（基座另生成，和 execd 令牌不是同一个）
SERVICE_TOKEN_ENV = "ATLAS_BRIDGE_TOKEN"
#: 会话 Pod 模板把 Secret atlas-cli-model 挂在这里（sandbox/base/lifecycle/seed/cli.yaml）
MODEL_KEY_FILE = "/run/secrets/atlas/model_api_key"
#: 经模型网关时，主体 Key 随 Pod 的机密环境变量进来（entrypoint 转成 CLI 认的变量）
MODEL_KEY_ENV = "ATLAS_MODEL_KEY"
#: bridge 传给 agent 的基础环境变量（白名单；其余的随用途追加）
_AGENT_ENV_BASE = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "TERM")


class PodEndpoint(NamedTuple):
    """连上一个会话 Pod 需要的全部信息：bridge 的 ws 地址和服务令牌。

    ★ 令牌随 Pod 生灭：重建就换新，所以跟着这次拿到的 Pod 一起返回，而不是从配置里读一个长期值。
    """

    url: str
    token: str


class CliModel(NamedTuple):
    """经模型网关时交给 Pod 里 CLI 的模型接入。

    ★ 只有网关地址、主体 Key 和模型名：供应商 Key 不进 Pod。
    """

    base_url: str
    key: str
    model: str
    #: CLI 做标题这类旁路工作的轻量模型：也要指向主体有权的模型
    small_model: str
    #: 只作用量归属参考的请求头（会话级：使用者、Agent）
    headers: dict[str, str]


@runtime_checkable
class PodProvider(Protocol):
    async def ensure(
        self,
        thread: Thread,
        cli: CliSpec,
        *,
        model: CliModel | None = None,
        credentials: CredentialSet | None = None,
    ) -> PodEndpoint:
        """确保这个会话的 Pod 在跑，返回连 bridge 的地址和令牌。每轮调一次，不在外面缓存。"""
        ...


@dataclass(frozen=True)
class PodRequest:
    thread_uuid: UUID
    #: 工作区所属的根会话（子智能体与主 agent 共享）
    workspace_uuid: UUID
    user_uuid: UUID
    cli: CliSpec
    model: CliModel | None = None
    credentials: CredentialSet = field(default_factory=CredentialSet)

    @property
    def layout(self) -> WorkspaceLayout:
        return WorkspaceLayout.of(self.user_uuid, self.thread_uuid, self.workspace_uuid)


def _entrypoint(*, model_key: bool, gateway_key: bool = False) -> list[str]:
    """一小段 sh：令牌写成 bridge 认的文件、挂进来的模型 key 读成 CLI 认的变量，再 exec bridge。"""
    script = f'umask 077; printf %s "${SERVICE_TOKEN_ENV}" > {TOKEN_FILE}; umask 022; '
    unset = f"-u {SERVICE_TOKEN_ENV}"
    if gateway_key:
        # 模型网关的主体 Key（doc/model-design.html §8.1）：不再是挂进来的供应商 Key
        script += f'ANTHROPIC_AUTH_TOKEN="${MODEL_KEY_ENV}"; export ANTHROPIC_AUTH_TOKEN; '
        unset += f" -u {MODEL_KEY_ENV}"
    elif model_key:
        script += (
            f'if [ -r {MODEL_KEY_FILE} ]; then ANTHROPIC_AUTH_TOKEN="$(cat {MODEL_KEY_FILE})"; '
            "export ANTHROPIC_AUTH_TOKEN; fi; "
        )
    # ★ 令牌不留在 bridge 的环境里（它已经在文件里了）
    return ["/bin/sh", "-c", script + f"exec env {unset} python -m atlas_bridge"]


def _git_env(author: GitAuthor | None, trust: MitmTrust) -> dict[str, str]:
    """git 的身份与行为。凭据不在这里 —— 在 Credential Vault 里。"""
    env = {
        # 没有凭据时立刻失败，而不是卡在交互式输入用户名上（Pod 里没有终端）
        "GIT_TERMINAL_PROMPT": "0",
        **trust.env(),  # git 不认 SSL_CERT_FILE：不另给 GIT_SSL_CAINFO 就过不了 MITM
        # 原来写在 gitconfig 里的两条（git ≥ 2.31 的环境变量配置）
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "init.defaultBranch",
        "GIT_CONFIG_VALUE_0": "master",
        "GIT_CONFIG_KEY_1": "pull.rebase",
        "GIT_CONFIG_VALUE_1": "true",
    }
    if author is not None:
        env |= {
            "GIT_AUTHOR_NAME": author.name,
            "GIT_AUTHOR_EMAIL": author.email,
            "GIT_COMMITTER_NAME": author.name,
            "GIT_COMMITTER_EMAIL": author.email,
        }
    return env


class CliPodKind:
    """会话 Pod 这一种。无状态：装配时按 biz_type = CLI_AGENT 登记到通用供给。"""

    biz_type = BizType.CLI_AGENT

    def __init__(
        self,
        settings: Settings,
        runtime: SandboxRuntime,
        egress: EgressControl,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._runtime = runtime
        self._egress = egress
        self._transport = transport

    async def admit(self, req: PodRequest) -> None:
        if not req.cli.image:
            raise ValueError("CLI 智能体的 cli.image 为空，无法创建会话 Pod")

    def build(self, req: PodRequest, biz: BizRef) -> CreateSpec:
        s = self._settings
        cli, model, layout = req.cli, req.model, req.layout
        claude = cli.cli_type == "claude-code" and bool(s.cli_model_base_url) and model is None
        agent_env: dict[str, str] = {}
        secret_env: dict[str, SecretStr] = {}
        allow = list(_AGENT_ENV_BASE)
        if model is not None:
            agent_env["ANTHROPIC_BASE_URL"] = model.base_url
            agent_env["ANTHROPIC_MODEL"] = model.model
            agent_env["ANTHROPIC_SMALL_FAST_MODEL"] = model.small_model
            # ★ 新版 Claude Code 按别名（sonnet / opus / haiku、SubAgent）找模型，不再只看上面两个：
            #   不指的话它会用内置的模型名，网关目录里没有，被拒（§8.1）
            agent_env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = model.model
            agent_env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = model.model
            agent_env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = model.small_model
            agent_env["CLAUDE_CODE_SUBAGENT_MODEL"] = model.model
            if model.headers:
                agent_env["ANTHROPIC_CUSTOM_HEADERS"] = "\n".join(
                    f"{k}: {v}" for k, v in model.headers.items()
                )
            secret_env[MODEL_KEY_ENV] = SecretStr(model.key)
            allow.append("ANTHROPIC_AUTH_TOKEN")  # entrypoint 从 ATLAS_MODEL_KEY 转过来
        if claude:
            # base_url 指向任何 Anthropic 兼容端点即可
            agent_env["ANTHROPIC_BASE_URL"] = s.cli_model_base_url
            if s.cli_model_name:
                agent_env["ANTHROPIC_MODEL"] = s.cli_model_name
                # 小模型也指过去：不指的话 CLI 会去找端点上不存在的 haiku
                agent_env["ANTHROPIC_SMALL_FAST_MODEL"] = s.cli_model_name
            allow.append("ANTHROPIC_AUTH_TOKEN")  # entrypoint 从文件读出来
        network = NetworkPolicy.allow_all()
        if not req.credentials.empty():
            # ★ 凭据注入只能在建 Pod 时开：会话的档案选择建会话时定、之后不改
            network = network.with_credentials()
            trust = network.trust()
            assert trust is not None
            agent_env.update(_git_env(req.credentials.git_author, trust))
            allow.extend(trust.inherited)  # bootstrap 导出的 CA 变量要原样传给 CLI
        allow.extend(agent_env)
        env = {
            "EXECD_ENVS": "/tmp/.execd/env",
            "ATLAS_BRIDGE_TOKEN_FILE": TOKEN_FILE,
            # 会话绑定：握手头 X-Session-Id 必须等于它，令牌对也不行（Bridge 设计 §4.3）
            "ATLAS_BRIDGE_SESSION_ID": str(req.thread_uuid),
            "ATLAS_BRIDGE_LISTEN_PORT": str(s.cli_bridge_port),
            "ATLAS_BRIDGE_AGENT_CMD": cli.adapter,
            "ATLAS_BRIDGE_WORKSPACE": WORKSPACE_MOUNT,
            # agent 只拿到白名单里的变量（§8.3）
            "ATLAS_BRIDGE_AGENT_ENV_ALLOW": ",".join(dict.fromkeys(allow)),
            "HOME": STATE_MOUNT,
            **agent_env,
        }
        return CreateSpec(
            template=CLI_TEMPLATE,
            image=cli.image,
            entrypoint=tuple(_entrypoint(model_key=claude, gateway_key=model is not None)),
            network=network,
            resources=Resources(
                s.cli_cpu_request, s.cli_cpu_limit, s.cli_memory_request, s.cli_memory_limit
            ),
            env=env,
            secret_env=secret_env,
            service_token_env=SERVICE_TOKEN_ENV,
            volumes=(
                # ★ 工作区挂根会话的前缀（子智能体与主 agent 共享），技能与 CLI 的 HOME 挂自己的
                Volume("workspace", s.sandbox_pvc, layout.prefix(Area.WORKSPACE), WORKSPACE_MOUNT),
                Volume(
                    "skills",
                    s.sandbox_pvc,
                    layout.prefix(Area.SKILLS),
                    SKILLS_MOUNT,
                    read_only=True,
                ),
                Volume("state", s.sandbox_pvc, layout.key(Area.SYSTEM, "cli-home"), STATE_MOUNT),
            ),
            labels=Labels.of(user=str(biz.user_uuid)),
        )

    async def attach(self, ref: SandboxRef, req: PodRequest, *, created: bool) -> None:
        """每轮把凭据整份写一遍：崩在「建好」和「写凭据」之间的能补上，换了令牌下一轮生效。"""
        if not req.credentials.empty():
            await self._egress.bind(ref.sandbox_uuid, list(req.credentials.bindings))

    async def ready(self, ref: SandboxRef) -> None:
        """bridge 的 /readyz 回 200（拉起 CLI 并完成初始化）。

        ★ 每 5 秒问一次沙箱还在不在：bridge 或 CLI 起不来时容器退出、沙箱变 Failed，不用等满时限。
        """
        deadline = time.monotonic() + self._settings.cli_ready_timeout_s
        next_check = time.monotonic() + 5
        async with httpx.AsyncClient(
            timeout=5.0, transport=self._transport, trust_env=False
        ) as client:
            while True:
                try:
                    ep = await self._runtime.endpoint(
                        ref.sandbox_uuid, self._settings.cli_bridge_port
                    )
                    if (await client.get(f"{ep.url}/readyz")).status_code == 200:
                        return
                except httpx.TransportError:
                    pass
                now = time.monotonic()
                if now > deadline:
                    raise TimeoutError(f"会话 Pod {ref.sandbox_uuid} 的 bridge 在时限内没有就绪")
                if now > next_check:
                    next_check = now + 5
                    instance = await self._runtime.get(ref.sandbox_uuid)
                    if instance is None or not instance.usable():
                        raise RuntimeError(
                            f"会话 Pod {ref.sandbox_uuid} 启动失败（bridge 或 CLI 退出）"
                        )
                await asyncio.sleep(0.5)


#: 会话还有没有没结束的 run、最近一次结束在什么时候（Run 执行器那边的查询）
OpenRunQuery = Callable[[UUID], Awaitable[tuple[bool, datetime | None]]]


class SessionPods:
    """会话 Pod 的流程（PodProvider 的实现）。闲置回收、删除级联、孤儿回收都只释放自己的业务。"""

    def __init__(
        self,
        provisioning: SandboxProvisioning,
        runtime: SandboxRuntime,
        settings: Settings,
        *,
        runs: OpenRunQuery | None = None,
    ) -> None:
        self._prov = provisioning
        self._runtime = runtime
        self._settings = settings
        self._runs = runs

    async def ensure(
        self,
        thread: Thread,
        cli: CliSpec,
        *,
        model: CliModel | None = None,
        credentials: CredentialSet | None = None,
    ) -> PodEndpoint:
        """每轮都经通用供给确认一遍（bridge 崩了沙箱会变 Failed，供给就退役它、重建一个）。"""
        req = PodRequest(
            thread_uuid=thread.thread_uuid,
            workspace_uuid=thread.workspace_thread_id,
            user_uuid=thread.created_by_uuid,
            cli=cli,
            model=model,
            credentials=credentials or CredentialSet(),
        )
        ref = await self._prov.ensure(_biz(thread.thread_uuid, thread.created_by_uuid), req)
        ep = await self._runtime.endpoint(ref.sandbox_uuid, self._settings.cli_bridge_port)
        token = await self._runtime.service_token(ref.sandbox_uuid)
        if token is None:
            raise RuntimeError(f"会话 Pod {ref.sandbox_uuid} 没有 bridge 服务令牌")
        return PodEndpoint("ws://" + ep.url.removeprefix("http://"), token.get_secret_value())

    async def release(self, thread_uuid: UUID, user_uuid: UUID) -> None:
        """会话删除级联（reason=release）。Pod 没有别的业务了才退役；失败只记日志。"""
        await self._prov.release(_biz(thread_uuid, user_uuid), reason="release")

    async def reap_idle(self) -> int:
        """会话没有没结束的 run、且最近一次结束超过时限：释放（reason=idle）。

        挂起、等审批都算没结束。

        ★ 看 run 的状态，不看 execd 的活动：CLI 的流量走 bridge，不经过 execd。Pod 重建代价低：
          HOME 持久化、工作区在对象存储，下一轮 CLI 从 HOME 接着跑。
        """
        if self._runs is None:
            return 0
        horizon = datetime.now(UTC) - timedelta(seconds=self._settings.cli_pod_idle_ttl_s)
        released = 0
        for biz, _ in await self._prov.serving(BizType.CLI_AGENT):
            open_run, last_ended = await self._runs(biz.biz_id)
            if open_run or (last_ended is not None and last_ended > horizon):
                continue
            logger.info("会话 %s 的 Pod 闲置，释放", biz.biz_id)
            await self._prov.release(biz, reason="idle")
            released += 1
        return released

    async def reap_orphans(self, live_threads: Iterable[UUID]) -> int:
        """会话已不在或已归档的，一律释放（reason=orphan）。"""
        return await self._prov.reap_orphans(BizType.CLI_AGENT, live_threads)


def _biz(thread_uuid: UUID, user_uuid: UUID) -> BizRef:
    return BizRef(BizType.CLI_AGENT, thread_uuid, user_uuid)
