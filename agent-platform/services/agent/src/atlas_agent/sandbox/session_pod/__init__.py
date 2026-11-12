"""沙箱 · 会话 Pod（c4-model/agent/sandbox/code-sandbox-session-pod.html）。

外部 CLI 智能体的运行环境。一个人的 CLI 会话共用 Pod（同镜像、同凭据集合，一个 Pod 至多
cli_sessions_per_pod 个会话），Pod 里按会话起各自的 bridge + CLI：

    /data                  整个 {user}/ 读写挂进来：工作区、技能、CLI 的会话记录都在下面
    bridge（每会话一个）   端口 = cli_bridge_port + 槽位号，cwd = /data/{根会话}/workspace；
                           由 Atlas 经 execd 在后台拉起，会话释放、闲置时 SIGTERM（排空后退出）
    CLAUDE_CONFIG_DIR      /data/home/.claude：一人一份。Pod 重建后 cwd 不变，
                           CLI 接着原来的会话
    HOME                   /tmp/home：本地盘，快，重建后清空（缓存、装的包）

    PodProvider · PodEndpoint   CLI 运行时拿会话 bridge 的唯一接口：ws 地址和服务令牌
    CliModel · PodRequest       这一轮的模型接入、要 Pod 的会话信息
    CliPodKind                  会话 Pod 这一种（SandboxKind）：模板 cli；按凭据集合共享；
                                凭据非空时开凭据注入、每轮整份重写；就绪看 execd /ping
    BridgeLauncher              按会话确认 / 拉起 / 停掉 bridge
    SessionPods                 ensure · release · reap_idle · reap_orphans

★ Pod 的规格里没有任何会话特有的东西（会被这个人的别的 CLI 会话共用）：模型接入、git 作者都
  跟着每个会话的 bridge 走（execd 的命令环境变量）；镜像、凭据集合不同的会话不共用 Pod。
★ 交出 bridge 服务令牌，不交 execd 令牌：拿到它也不能在 Pod 里执行任意命令。令牌一个 Pod 一个，
  各会话的 bridge 都用它；握手头 X-Session-Id 必须等于 bridge 自己的会话，所以连不到别的会话。
★ 会话之间不隔离：同一个 Pod 里的 CLI 能读写别的会话的文件（共享的前提，用户 2026-11-04 定的）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shlex
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, NamedTuple, Protocol, runtime_checkable
from uuid import UUID

import httpx
from atlas_host import HEADER_SESSION

from ...credentials import CredentialSet, GitAuthor
from ...domain.workspace_layout import DATA_MOUNT, HOME_CLAUDE, WorkspaceLayout
from ..base.execution.ports import SandboxExec
from ..base.execution.values import CommandGone, CommandSpec
from ..base.lifecycle.runtime import DestroyReason, SandboxRuntime
from ..base.lifecycle.spec import CreateSpec, Labels, Resources, Volume
from ..base.network.egress import EgressControl
from ..base.network.policy import MitmTrust, NetworkPolicy
from ..provisioning.model import BizRef, BizType, SandboxRef, Sharing
from ..provisioning.service import SandboxProvisioning, SandboxUnavailableError

if TYPE_CHECKING:
    from atlas_engine.contracts.spec import CliSpec

    from ...config import Settings
    from ...db.models import Thread

__all__ = [
    "CLI_TEMPLATE",
    "BridgeLauncher",
    "CliModel",
    "CliPodKind",
    "PodEndpoint",
    "PodProvider",
    "PodRequest",
    "SessionPods",
]

logger = logging.getLogger(__name__)

CLI_TEMPLATE = "cli"
#: CLI 的 HOME：本地盘（模板里 /tmp 是 emptyDir），重建后清空。要跨重建留下的只有会话记录和配置，
#: 它们在 CLAUDE_CONFIG_DIR（/data/home/.claude）
LOCAL_HOME = "/tmp/home"
#: 每个会话的 bridge 的令牌、pid、日志文件放在这里（/tmp 是 emptyDir）
BRIDGE_DIR = "/tmp/atlas-bridge"
#: bridge 的服务令牌放在 Pod 的这个环境变量里（基座另生成，和 execd 令牌不是同一个）。
#: 拉起 bridge 的脚本把它写成 bridge 认的文件，再从 bridge 的环境里去掉
SERVICE_TOKEN_ENV = "ATLAS_BRIDGE_TOKEN"
#: 会话 Pod 模板把 Secret atlas-cli-model 挂在这里（sandbox/base/lifecycle/seed/cli.yaml）
MODEL_KEY_FILE = "/run/secrets/atlas/model_api_key"
#: 经模型网关时，主体 Key 随这个会话 bridge 的环境变量进来（拉起脚本转成 CLI 认的变量）。
#: ★ 不放进 Pod 的规格：主体 Key 按智能体分，同一个 Pod 里的会话可能用不同的智能体
MODEL_KEY_ENV = "ATLAS_MODEL_KEY"
#: bridge 传给 agent 的基础环境变量（白名单；其余的随用途追加）
_AGENT_ENV_BASE = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "TERM", "CLAUDE_CONFIG_DIR")


class PodEndpoint(NamedTuple):
    """连上一个会话的 bridge 需要的全部信息：ws 地址和服务令牌。

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
    #: False = 让 CLI 关掉提示词缓存（不发 cache_control）：模型不支持缓存，或智能体关了它。
    #: ★ 不关的话 LiteLLM 会把 cache_control 翻成 Gemini 的上下文缓存，免费档额度为 0，整轮 429
    prompt_cache: bool = True


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
        """确保这个会话的 bridge 在跑，返回连它的地址和令牌。每轮调一次，不在外面缓存。"""
        ...


@dataclass(frozen=True)
class PodRequest:
    thread_uuid: UUID
    #: 会话的工作目录（相对 files/；子智能体与主 agent 相同）：CLI 的 cwd
    workspace_dir: str
    user_uuid: UUID
    cli: CliSpec
    model: CliModel | None = None
    credentials: CredentialSet = field(default_factory=CredentialSet)

    @property
    def layout(self) -> WorkspaceLayout:
        return WorkspaceLayout.of(self.user_uuid, self.thread_uuid, self.workspace_dir)


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


def _credential_key(credentials: CredentialSet) -> str:
    """凭据集合的共享键：凭据注入整个 Pod 一份（Vault 每轮整份重写），集合不同的会话不能共用。"""
    if credentials.empty():
        return "creds:"
    digest = hashlib.sha256(",".join(sorted(credentials.codes)).encode()).hexdigest()[:16]
    return f"creds:{digest}"


class CliPodKind:
    """会话 Pod 这一种。无状态：装配时按 biz_type = CLI_AGENT 登记到通用供给。

    ★ 规格里只有和会话无关的东西：模板、镜像、网络（凭据注入开不开）、整个 {user}/ 的挂载。Pod 的
      入口是 execd（tail 占住），bridge 由 BridgeLauncher 按会话拉起。
    """

    biz_type = BizType.CLI_AGENT

    def __init__(
        self,
        settings: Settings,
        egress: EgressControl,
        exec: SandboxExec,
    ) -> None:
        self._settings = settings
        self._egress = egress
        self._exec = exec

    async def admit(self, req: PodRequest) -> None:
        if not req.cli.image:
            raise ValueError("CLI 智能体的 cli.image 为空，无法创建会话 Pod")

    def share(self, req: PodRequest) -> Sharing:
        """同镜像（供给拼进键里）、同凭据集合的会话共用；一个 Pod 至多 cli_sessions_per_pod 个。"""
        return Sharing(_credential_key(req.credentials), self._settings.cli_sessions_per_pod)

    def build(self, req: PodRequest, biz: BizRef) -> CreateSpec:
        s = self._settings
        network = NetworkPolicy.allow_all()
        if not req.credentials.empty():
            # ★ 凭据注入只能在建 Pod 时开：会话的档案选择建会话时定、之后不改
            network = network.with_credentials()
        return CreateSpec(
            template=CLI_TEMPLATE,
            image=req.cli.image,
            entrypoint=("tail", "-f", "/dev/null"),
            network=network,
            resources=Resources(
                s.cli_cpu_request, s.cli_cpu_limit, s.cli_memory_request, s.cli_memory_limit
            ),
            env={
                "EXECD_INIT": "1",  # execd 作 PID 1：回收僵尸进程（bridge 拉起的 CLI）、转发信号
                "EXECD_ENVS": "/tmp/.execd/env",
                "TMPDIR": "/tmp",
                "HOME": LOCAL_HOME,
            },
            service_token_env=SERVICE_TOKEN_ENV,
            # ★ 唯一的挂载：整个 {user}/ 读写挂到 /data（工作区、技能、CLI 的会话记录）
            volumes=(Volume("data", s.sandbox_pvc, req.layout.user_prefix, DATA_MOUNT),),
            labels=Labels.of(user=str(biz.user_uuid)),
        )

    async def attach(self, ref: SandboxRef, req: PodRequest, *, created: bool) -> None:
        """每轮把凭据整份写一遍：崩在「建好」和「写凭据」之间的能补上，换了令牌下一轮生效。

        共用这个 Pod 的会话凭据集合相同（共享键），写的是同一份。
        """
        if not req.credentials.empty():
            await self._egress.bind(ref.sandbox_uuid, list(req.credentials.bindings))

    async def ready(self, ref: SandboxRef) -> None:
        """execd 的 /ping 回话才算就绪（bridge 按会话另起，就绪由 BridgeLauncher 等）。"""
        deadline = time.monotonic() + self._settings.cli_ready_timeout_s
        while not await self._exec.ping(ref.sandbox_uuid):
            if time.monotonic() > deadline:
                raise TimeoutError(f"会话 Pod {ref.sandbox_uuid} 的 execd 没有就绪")
            await asyncio.sleep(0.5)


@dataclass(frozen=True)
class _Probe:
    """端口上的 bridge：有没有、是不是这个会话的、就绪没有。"""

    present: bool
    session: str | None = None
    ready: bool = False


class BridgeLauncher:
    """一个会话的 bridge：确认它在跑（不在就经 execd 拉起、等就绪），或者停掉它。

    ★ 端口 = cli_bridge_port + 槽位号：槽位在账户聚合里分配，同一个 Pod 里各不相同。
    ★ 端口上的是不是自己的 bridge，看 /readyz 的响应头 X-Session-Id；是别的会话的（释放时没停掉），
      先停掉它再起自己的。
    ★ bridge 会话结束就退出（不再带走 Pod）：下一轮来时端口上没人，重新拉起，
      CLI 按记下的会话 id 接着跑。
    """

    #: 等就绪时，隔多久看一次拉起的命令还在不在跑
    CHECK_EVERY_S = 5.0

    def __init__(
        self,
        settings: Settings,
        runtime: SandboxRuntime,
        exec: SandboxExec,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._runtime = runtime
        self._exec = exec
        self._transport = transport

    def port(self, slot: int) -> int:
        return self._settings.cli_bridge_port + slot

    async def ensure(self, ref: SandboxRef, slot: int, req: PodRequest) -> PodEndpoint:
        session = str(req.thread_uuid)
        ep = await self._runtime.endpoint(ref.sandbox_uuid, self.port(slot))
        token = await self._runtime.service_token(ref.sandbox_uuid)
        if token is None:
            raise RuntimeError(f"会话 Pod {ref.sandbox_uuid} 没有 bridge 服务令牌")
        probe = await self._probe(ep.url)
        if probe.present and probe.session != session:
            logger.warning(
                "会话 Pod %s 的端口 %d 上是会话 %s 的 bridge，先停掉",
                ref.sandbox_uuid,
                self.port(slot),
                probe.session,
            )
            if probe.session:
                await self.stop(ref, probe.session)
            await self._await_free(ep.url)
            probe = _Probe(present=False)
        cmd_id = None if probe.present else await self._launch(ref, slot, req)
        if not probe.ready:
            await self._await_ready(ref, ep.url, session, cmd_id)
        return PodEndpoint("ws://" + ep.url.removeprefix("http://"), token.get_secret_value())

    async def stop(self, ref: SandboxRef, session: str) -> None:
        """SIGTERM：bridge 排空（当前一轮以 pod_terminating 收尾、关掉 CLI）后退出。不等它退出。"""
        pid = f"{BRIDGE_DIR}/{session}.pid"
        command = f'[ -f {pid} ] && kill -TERM "$(cat {pid})" 2>/dev/null; rm -f {pid}; true'
        try:
            await self._exec.spawn(ref.sandbox_uuid, CommandSpec(command, cwd="/tmp"))
        except Exception:
            logger.warning("停会话 %s 的 bridge 失败", session, exc_info=True)

    # ------------------------------------------------------------------ 内部

    async def _launch(self, ref: SandboxRef, slot: int, req: PodRequest) -> str:
        script, env = self._command(slot, req)
        logger.info("在会话 Pod %s 里拉起会话 %s 的 bridge", ref.sandbox_uuid, req.thread_uuid)
        # ★ cwd 用本地盘：工作区目录在共用的 Pod 里可能还不存在（新会话还没写过文件），脚本先建好
        return await self._exec.spawn(ref.sandbox_uuid, CommandSpec(script, cwd="/tmp", env=env))

    def _command(self, slot: int, req: PodRequest) -> tuple[str, dict[str, str]]:
        """拉起 bridge 的一段 sh 和它的环境变量。

        令牌写成 bridge 认的文件、模型 key 转成 CLI 认的变量，记下 pid，再 exec bridge（pid 不变）；
        ★ 令牌和模型 key 不留在 bridge 的环境里（bridge 只把白名单里的变量传给 CLI，
          这里再去掉一层）。
        """
        s = self._settings
        cli, model, layout = req.cli, req.model, req.layout
        session = str(req.thread_uuid)
        claude = cli.cli_type == "claude-code" and bool(s.cli_model_base_url) and model is None
        agent_env: dict[str, str] = {}
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
            if not model.prompt_cache:
                agent_env["DISABLE_PROMPT_CACHING"] = "1"
            if model.headers:
                agent_env["ANTHROPIC_CUSTOM_HEADERS"] = "\n".join(
                    f"{k}: {v}" for k, v in model.headers.items()
                )
            allow.append("ANTHROPIC_AUTH_TOKEN")  # 脚本从 ATLAS_MODEL_KEY 转过来
        if claude:
            # base_url 指向任何 Anthropic 兼容端点即可
            agent_env["ANTHROPIC_BASE_URL"] = s.cli_model_base_url
            if s.cli_model_name:
                agent_env["ANTHROPIC_MODEL"] = s.cli_model_name
                # 小模型也指过去：不指的话 CLI 会去找端点上不存在的 haiku
                agent_env["ANTHROPIC_SMALL_FAST_MODEL"] = s.cli_model_name
            allow.append("ANTHROPIC_AUTH_TOKEN")  # 脚本从挂进来的文件读出来
        if not req.credentials.empty():
            trust = NetworkPolicy.allow_all().with_credentials().trust()
            assert trust is not None
            agent_env.update(_git_env(req.credentials.git_author, trust))
            allow.extend(trust.inherited)  # bootstrap 导出的 CA 变量要原样传给 CLI
        allow.extend(agent_env)
        token_file = f"{BRIDGE_DIR}/{session}.token"
        env = {
            "ATLAS_BRIDGE_TOKEN_FILE": token_file,
            # 会话绑定：握手头 X-Session-Id 必须等于它，令牌对也不行（Bridge 设计 §4.3）
            "ATLAS_BRIDGE_SESSION_ID": session,
            "ATLAS_BRIDGE_LISTEN_PORT": str(self.port(slot)),
            "ATLAS_BRIDGE_AGENT_CMD": cli.adapter,
            # ★ CLI 的 cwd：会话的工作目录。CLI 按 cwd 找会话记录，工作目录建会话时就定下、
            #   之后不变，Pod 重建前后一样
            "ATLAS_BRIDGE_WORKSPACE": layout.workdir,
            # agent 只拿到白名单里的变量（§8.3）
            "ATLAS_BRIDGE_AGENT_ENV_ALLOW": ",".join(dict.fromkeys(allow)),
            "HOME": LOCAL_HOME,
            "CLAUDE_CONFIG_DIR": layout.real(HOME_CLAUDE),
            **agent_env,
        }
        q = shlex.quote
        script = (
            'mkdir -p "$ATLAS_BRIDGE_WORKSPACE" "$CLAUDE_CONFIG_DIR" '
            f"{LOCAL_HOME} {BRIDGE_DIR} || exit 1; "
            f'umask 077; printf %s "${SERVICE_TOKEN_ENV}" > {q(token_file)}; umask 022; '
        )
        unset = f"-u {SERVICE_TOKEN_ENV}"
        if model is not None:
            # 模型网关的主体 Key（doc/model-design.html §8.1）
            env[MODEL_KEY_ENV] = model.key
            script += f'ANTHROPIC_AUTH_TOKEN="${MODEL_KEY_ENV}"; export ANTHROPIC_AUTH_TOKEN; '
            unset += f" -u {MODEL_KEY_ENV}"
        elif claude:
            script += (
                f'if [ -r {MODEL_KEY_FILE} ]; then ANTHROPIC_AUTH_TOKEN="$(cat {MODEL_KEY_FILE})"; '
                "export ANTHROPIC_AUTH_TOKEN; fi; "
            )
        log = q(f"{BRIDGE_DIR}/{session}.log")
        script += (
            f"echo $$ > {q(f'{BRIDGE_DIR}/{session}.pid')}; "
            f"exec env {unset} python -m atlas_bridge >> {log} 2>&1"
        )
        return script, env

    async def _probe(self, url: str) -> _Probe:
        try:
            async with self._client() as client:
                resp = await client.get(f"{url}/readyz")
        except httpx.TransportError:
            return _Probe(present=False)
        return _Probe(
            present=True, session=resp.headers.get(HEADER_SESSION), ready=resp.status_code == 200
        )

    async def _await_free(self, url: str) -> None:
        """等端口上的旧 bridge 退出（它先排空再退）。"""
        deadline = time.monotonic() + self._settings.cli_ready_timeout_s
        while (await self._probe(url)).present:
            if time.monotonic() > deadline:
                raise SandboxUnavailableError("会话 Pod 里旧的 bridge 一直没有退出")
            await asyncio.sleep(0.5)

    async def _await_ready(
        self, ref: SandboxRef, url: str, session: str, cmd_id: str | None
    ) -> None:
        """/readyz 回 200（拉起 CLI 并完成初始化）。

        ★ 每 5 秒看一次拉起的命令还在不在跑：bridge 或 CLI 起不来时进程退出，不用等满时限。
        """
        deadline = time.monotonic() + self._settings.cli_ready_timeout_s
        next_check = time.monotonic() + self.CHECK_EVERY_S
        while True:
            probe = await self._probe(url)
            if probe.ready and probe.session == session:
                return
            now = time.monotonic()
            if now > deadline:
                raise TimeoutError(f"会话 {session} 的 bridge 在时限内没有就绪")
            if cmd_id is not None and now >= next_check:
                next_check = now + self.CHECK_EVERY_S
                try:
                    status = await self._exec.status(ref.sandbox_uuid, cmd_id)
                except CommandGone:
                    status = None
                if status is None or not status.running:
                    raise RuntimeError(
                        f"会话 {session} 的 bridge 启动失败（bridge 或 CLI 退出，"
                        f"日志在 Pod 的 {BRIDGE_DIR}/{session}.log）"
                    )
            await asyncio.sleep(0.5)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=5.0, transport=self._transport, trust_env=False)


#: 会话还有没有没结束的 run、最近一次结束在什么时候（Run 执行器那边的查询）
OpenRunQuery = Callable[[UUID], Awaitable[tuple[bool, datetime | None]]]


class SessionPods:
    """会话 Pod 的流程（PodProvider 的实现）。闲置回收、删除级联、孤儿回收都只释放自己的业务。"""

    def __init__(
        self,
        provisioning: SandboxProvisioning,
        bridges: BridgeLauncher,
        settings: Settings,
        *,
        runs: OpenRunQuery | None = None,
    ) -> None:
        self._prov = provisioning
        self._bridges = bridges
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
        """每轮都经通用供给确认一遍 Pod（坏了退役、重建），再确认这个会话的 bridge 在跑。"""
        req = PodRequest(
            thread_uuid=thread.thread_uuid,
            workspace_dir=thread.workspace_dir,
            user_uuid=thread.created_by_uuid,
            cli=cli,
            model=model,
            credentials=credentials or CredentialSet(),
        )
        biz = _biz(thread.thread_uuid, thread.created_by_uuid)
        ref = await self._prov.ensure(biz, req)
        slot = await self._prov.slot(biz)
        if slot is None:
            raise SandboxUnavailableError(f"会话 {thread.thread_uuid} 刚分到的会话 Pod 已被退役")
        return await self._bridges.ensure(ref, slot, req)

    async def release(self, thread_uuid: UUID, user_uuid: UUID) -> None:
        """会话删除级联（reason=release）：停掉它的 bridge 再解绑。Pod 没有别的会话了才退役。"""
        await self._release(_biz(thread_uuid, user_uuid), reason="release")

    async def reap_idle(self) -> int:
        """会话没有没结束的 run、且最近一次结束超过时限：停掉 bridge、释放（reason=idle）。

        挂起、等审批都算没结束。

        ★ 看 run 的状态，不看 execd 的活动：CLI 的流量走 bridge，不经过 execd。重建代价低：
          会话记录在 /data/home/.claude、工作区在对象存储，下一轮 CLI 接着跑。
        """
        if self._runs is None:
            return 0
        horizon = datetime.now(UTC) - timedelta(seconds=self._settings.cli_pod_idle_ttl_s)
        released = 0
        for biz, _ in await self._prov.serving(BizType.CLI_AGENT):
            open_run, last_ended = await self._runs(biz.biz_id)
            if open_run or (last_ended is not None and last_ended > horizon):
                continue
            logger.info("会话 %s 闲置，停掉它的 bridge、释放", biz.biz_id)
            await self._release(biz, reason="idle")
            released += 1
        return released

    async def reap_orphans(self, live_threads: Iterable[UUID]) -> int:
        """会话已不在或已归档的，一律释放（reason=orphan）。"""
        live = set(live_threads)
        orphans = [biz for biz, _ in await self._prov.serving(BizType.CLI_AGENT)]
        for biz in orphans:
            if biz.biz_id not in live:
                await self._stop(biz)
        return await self._prov.reap_orphans(BizType.CLI_AGENT, live)

    async def _release(self, biz: BizRef, *, reason: DestroyReason) -> None:
        await self._stop(biz)
        await self._prov.release(biz, reason=reason)

    async def _stop(self, biz: BizRef) -> None:
        """Pod 还在别的会话手里：只停这个会话的 bridge（Pod 要退役的话，停不停都一样）。"""
        ref = await self._prov.current(biz)
        if ref is not None:
            await self._bridges.stop(ref, str(biz.biz_id))


def _biz(thread_uuid: UUID, user_uuid: UUID) -> BizRef:
    return BizRef(BizType.CLI_AGENT, thread_uuid, user_uuid)
