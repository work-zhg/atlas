"""ACP Pod（CLI + adapter + bridge）—— PodProvider 的 OpenSandbox 实现。

doc/sandbox-opensandbox-design.html §11、§12。

    OpenSandboxPods   HostRuntime 用的 PodProvider：ensure(thread, cli) → ws://PodIP:8900 + 令牌
    AcpProvisioner    opensandbox-acp 上的供给：按会话复用、git 档案授权、Credential Vault、
                      等 bridge 就绪

★ entrypoint 是一小段 sh：把 env 里的令牌写成 bridge 认的令牌文件、把挂进来的模型 key 读成 CLI 认的
  环境变量，再 exec bridge。bridge 因此一行不改，已有的 CLI 镜像照用（设计 §11.2 原计划改 bridge）。
★ bridge 令牌兼作 execd 令牌：execd 每个沙箱都会注入，不设令牌就是集群内无鉴权的命令执行入口。
  同 UID 的 CLI 本来就读得到 /proc/1/environ，分成两个令牌挡不住什么（§12）。
★ 有 git 档案时：networkPolicy(allow) + credentialProxy → egress sidecar 透明 MITM；
  ensure 时把凭据写进 Credential Vault。MITM 拦的是全部 HTTPS（P0-3 实测），
  所以 CLI 及其子进程都要信任 MITM 的 CA。
★ 不在 ensure 外缓存端点：每一轮都在锁里查一遍沙箱状态（HostRuntime 每轮调一次，频率很低），
  bridge 崩了（restartPolicy: Never → Failed）的那一轮之后就会重建，CLI 从 /state resume。
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import TYPE_CHECKING, Any

import httpx

from ..pods import AcpModel, PodEndpoint
from .lifecycle import LifecycleError
from .provision import Provisioner, SandboxEndpoint, pvc_volume

if TYPE_CHECKING:
    import redis.asyncio as aioredis

    from ...config import GitProfile, Settings
    from ...db.models import Thread
    from ...domain.spec import CliSpec

__all__ = ["AcpProvisioner", "GitProfileDenied", "OpenSandboxPods", "acp_spec"]

logger = logging.getLogger(__name__)

EGRESS_PORT = 18080
WORKSPACE_MOUNT = "/workspace"
SKILLS_MOUNT = "/skills"
#: CLI 的 HOME。★ 必须持久化：resume 靠它（CLI 把会话记录写在 HOME 下），而 Pod 会被删掉重建
STATE_MOUNT = "/state"
#: bridge 读令牌的文件（entrypoint 从 env 写出来；/tmp 是模板里的 emptyDir）
TOKEN_FILE = "/tmp/.atlas-bridge-token"
#: ACP 模板把 Secret atlas-acp-model 挂在这里（deploy/local/opensandbox/acp-template.yaml）
MODEL_KEY_FILE = "/run/secrets/atlas/model_api_key"
#: execd 的 bootstrap 把系统根证书与 MITM CA 合并成这份（P0-3 实测路径）
MERGED_CA = "/opt/opensandbox/merged-ca-certificates.pem"
#: bridge 传给 agent 的基础环境变量（白名单；其余的随用途追加）
_AGENT_ENV_BASE = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "TERM")
#: bootstrap 在开了 credentialProxy 时导出的 CA 变量：要原样传给 CLI
_CA_ENV = ("NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")


class GitProfileDenied(PermissionError):
    """请求的 git 凭据档案不存在，或会话无权使用它。"""


#: 经模型网关时，主体 Key 随 Pod 的环境变量进来（entrypoint 转成 CLI 认的变量）
MODEL_KEY_ENV = "ATLAS_MODEL_KEY"


def _entrypoint(*, model_key: bool, gateway_key: bool = False) -> list[str]:
    script = f'umask 077; printf %s "$ATLAS_BRIDGE_TOKEN" > {TOKEN_FILE}; umask 022; '
    unset = "-u ATLAS_BRIDGE_TOKEN"
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


def _git_env(profile: GitProfile) -> dict[str, str]:
    """git 的身份与行为。凭据不在这里 —— 在 Credential Vault 里。"""
    email = profile.author_email or f"{profile.username}@users.noreply.{profile.hosts[0]}"
    return {
        # 没有凭据时立刻失败，而不是卡在交互式输入用户名上（Pod 里没有终端）
        "GIT_TERMINAL_PROMPT": "0",
        # ★ git 不认 SSL_CERT_FILE（Debian 的 libcurl 编译期写死了 CA 路径）：不给就过不了 MITM
        "GIT_SSL_CAINFO": MERGED_CA,
        "CURL_CA_BUNDLE": MERGED_CA,
        "GIT_AUTHOR_NAME": profile.author_name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": profile.author_name,
        "GIT_COMMITTER_EMAIL": email,
        # 原来写在 gitconfig 里的两条（git ≥ 2.31 的环境变量配置）
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "init.defaultBranch",
        "GIT_CONFIG_VALUE_0": "master",
        "GIT_CONFIG_KEY_1": "pull.rebase",
        "GIT_CONFIG_VALUE_1": "true",
    }


def acp_spec(
    settings: Settings,
    *,
    thread: str,
    workspace_thread: str,
    user: str,
    token: str,
    cli: CliSpec,
    profile: GitProfile | None,
    model: AcpModel | None = None,
) -> dict[str, Any]:
    """ACP Pod 的创建请求（设计 §11.1）。三个前缀与 cluster 时代完全一致，存量数据不用迁移。

    model：经模型网关时的模型接入（网关地址、主体 Key、模型名）；None = 照旧按部署配置。
    """
    claude = cli.cli_type == "claude-code" and bool(settings.acp_model_base_url) and model is None
    agent_env: dict[str, str] = {}
    secret_env: dict[str, str] = {}
    allow = list(_AGENT_ENV_BASE)
    if model is not None:
        agent_env["ANTHROPIC_BASE_URL"] = model.base_url
        agent_env["ANTHROPIC_MODEL"] = model.model
        agent_env["ANTHROPIC_SMALL_FAST_MODEL"] = model.small_model
        if model.headers:
            # Claude Code 每个请求都带上（一行一个）：只作用量归属参考
            agent_env["ANTHROPIC_CUSTOM_HEADERS"] = "\n".join(
                f"{k}: {v}" for k, v in model.headers.items()
            )
        secret_env[MODEL_KEY_ENV] = model.key
        allow.append("ANTHROPIC_AUTH_TOKEN")  # entrypoint 从 ATLAS_MODEL_KEY 转过来
    if claude:
        # ★ base_url 指向任何 Anthropic 兼容端点即可（DeepSeek 的 /anthropic 能驱动真 Claude Code）
        agent_env["ANTHROPIC_BASE_URL"] = settings.acp_model_base_url
        if settings.acp_model_name:
            agent_env["ANTHROPIC_MODEL"] = settings.acp_model_name
            # 小模型也指过去：不指的话 CLI 会去找端点上不存在的 haiku（生成标题等旁路上失败）
            agent_env["ANTHROPIC_SMALL_FAST_MODEL"] = settings.acp_model_name
        allow.append("ANTHROPIC_AUTH_TOKEN")  # entrypoint 从文件读出来
    if profile is not None:
        agent_env.update(_git_env(profile))
        allow.extend(_CA_ENV)
    allow.extend(agent_env)

    spec: dict[str, Any] = {
        "image": {"uri": cli.image},
        "entrypoint": _entrypoint(model_key=claude, gateway_key=model is not None),
        "timeout": None,
        "resourceLimits": {"cpu": settings.acp_cpu_limit, "memory": settings.acp_memory_limit},
        "resourceRequests": {
            "cpu": settings.acp_cpu_request,
            "memory": settings.acp_memory_request,
        },
        "env": {
            "EXECD_ACCESS_TOKEN": token,
            "EXECD_ENVS": "/tmp/.execd/env",
            "ATLAS_BRIDGE_TOKEN": token,
            "ATLAS_BRIDGE_TOKEN_FILE": TOKEN_FILE,
            # 会话绑定：握手头 X-Session-Id 必须等于它，token 对也不行（Bridge 设计 §4.3）
            "ATLAS_BRIDGE_SESSION_ID": thread,
            "ATLAS_BRIDGE_LISTEN_PORT": str(settings.acp_bridge_port),
            "ATLAS_BRIDGE_AGENT_CMD": cli.adapter,
            "ATLAS_BRIDGE_WORKSPACE": WORKSPACE_MOUNT,
            # agent 只拿到白名单里的变量（§8.3）
            "ATLAS_BRIDGE_AGENT_ENV_ALLOW": ",".join(dict.fromkeys(allow)),
            "HOME": STATE_MOUNT,
            **agent_env,
            **secret_env,
        },
        "metadata": {
            "atlas.managed": "true",
            "atlas.kind": "acp",
            "atlas.thread": thread,
            "atlas.workspace": workspace_thread,
            "atlas.user": user,
        },
        "volumes": [
            # ★ 工作区挂父会话的前缀（子智能体与主 agent 共享），技能与状态挂自己的
            pvc_volume(
                settings, "workspace", f"{user}/{workspace_thread}/workspace", WORKSPACE_MOUNT
            ),
            pvc_volume(settings, "skills", f"{user}/{thread}/skills", SKILLS_MOUNT, read_only=True),
            pvc_volume(settings, "state", f"{user}/{thread}/system/cli-home", STATE_MOUNT),
        ],
    }
    if profile is not None:
        # Credential Vault 要透明 MITM，而 MITM 要一个网络策略。默认放行
        # （设计 §15：默认拒绝放到以后）
        spec["networkPolicy"] = {"defaultAction": "allow"}
        spec["credentialProxy"] = {"enabled": True}
    return spec


class AcpProvisioner(Provisioner):
    kind = "acp"

    @property
    def port(self) -> int:  # type: ignore[override]
        return self._settings.acp_bridge_port

    async def ensure(
        self, thread: Thread, cli: CliSpec, *, model: AcpModel | None = None
    ) -> SandboxEndpoint:
        if not cli.image:
            raise LifecycleError("acp agent 的 cli.image 为空，无法创建 Pod")
        key = str(thread.id)
        user = str(thread.created_by)
        profile = self._git_profile(thread)

        async def locked() -> SandboxEndpoint:
            previous = await self.routes.get(key, fresh=True)
            ep = await self._provision(
                key,
                user=user,
                previous=previous,
                admit=lambda: None,
                build=lambda token: acp_spec(
                    self._settings,
                    thread=key,
                    workspace_thread=str(thread.workspace_thread_id),
                    user=user,
                    token=token,
                    cli=cli,
                    profile=profile,
                    model=model,
                ),
                after_create=(lambda sid: self._bind_git(sid, profile)) if profile else None,
            )
            if profile is not None:
                await self._bind_git(ep.sandbox_id, profile, only_if_missing=True)
            return ep

        return await self._locked(key, locked)

    def _git_profile(self, thread: Thread) -> GitProfile | None:
        """★ 档案是凭据：谁能用由 Atlas 决定 —— 名字写错或无权一律拒绝，而不是静默不给
        （静默的表现是 git 在 Pod 里鉴权失败，原因很难追到这里）。"""
        name = thread.git_profile or ""
        if not name:
            return None
        profile = self._settings.git_profiles.get(name)
        if profile is None:
            raise GitProfileDenied(f"git 凭据档案 {name!r} 不存在")
        if profile.allowed_users and str(thread.created_by) not in profile.allowed_users:
            raise GitProfileDenied(f"会话创建者无权使用 git 凭据档案 {name!r}")
        source_app = thread.source_app or ""
        if profile.allowed_apps and source_app not in profile.allowed_apps:
            apps = "、".join(profile.allowed_apps)
            raise GitProfileDenied(f"只有 {apps} 建的会话能用 git 凭据档案 {name!r}")
        return profile

    async def _bind_git(
        self, sandbox_id: str, profile: GitProfile, *, only_if_missing: bool = False
    ) -> None:
        """把档案凭据写进沙箱的 Credential Vault（egress sidecar 的内存里，Pod 里看不到）。

        ★ egress 刚起来时可能还不监听：等到它回话。only_if_missing：复用时只补「建好后还没写进去」
          的那种（建沙箱的进程崩在两步之间）。
        """
        url, headers = await self.lifecycle.endpoint_with_headers(sandbox_id, EGRESS_PORT)
        cred = base64.b64encode(
            f"{profile.username}:{profile.token.get_secret_value()}".encode()
        ).decode()
        body = {
            "credentials": [{"name": "git", "source": {"type": "inline", "value": cred}}],
            "bindings": [
                {
                    "name": "git",
                    "match": {"hosts": profile.hosts},
                    "auth": {"type": "basic", "credential": "git"},
                }
            ],
        }
        deadline = time.monotonic() + self._settings.acp_ready_timeout_s
        async with httpx.AsyncClient(
            timeout=10.0, transport=self._transport, trust_env=False, headers=headers
        ) as client:
            while True:
                try:
                    if only_if_missing:
                        state = await client.get(f"{url}/credential-vault")
                        if state.status_code == 200 and (state.json() or {}).get("bindings"):
                            return
                    resp = await client.post(f"{url}/credential-vault", json=body)
                    if resp.status_code < 300:
                        logger.info("沙箱 %s 已写入 git 凭据（主机 %s）", sandbox_id, profile.hosts)
                        return
                    if resp.status_code < 500:
                        raise LifecycleError(
                            f"写 Credential Vault 失败：{resp.status_code} {resp.text[:200]}"
                        )
                except httpx.TransportError:
                    pass
                if time.monotonic() > deadline:
                    raise LifecycleError(f"沙箱 {sandbox_id} 的 egress 没有就绪，git 凭据写不进去")
                await asyncio.sleep(0.5)

    async def _ready(self, ep: SandboxEndpoint) -> None:
        """bridge 的 /readyz 回 200（拉起 agent + initialize 完成，Bridge 设计 §8.5）。

        ★ 每 5 秒问一次沙箱状态：bridge 起不来时容器退出、沙箱变 Failed，没必要等满超时。
        """
        deadline = time.monotonic() + self._settings.acp_ready_timeout_s
        next_check = time.monotonic() + 5
        async with httpx.AsyncClient(
            timeout=5.0, transport=self._transport, trust_env=False
        ) as client:
            while True:
                try:
                    if (await client.get(f"{ep.url}/readyz")).status_code == 200:
                        return
                except httpx.TransportError:
                    pass
                now = time.monotonic()
                if now > deadline:
                    raise LifecycleError(f"ACP Pod {ep.sandbox_id} 的 bridge 在时限内没有就绪")
                if now > next_check:
                    next_check = now + 5
                    if await self.is_gone(ep):
                        raise LifecycleError(
                            f"ACP Pod {ep.sandbox_id} 启动失败（bridge 或 CLI 退出）"
                        )
                await asyncio.sleep(0.5)


class OpenSandboxPods:
    """PodProvider：一个进程一个，HostRuntime 每轮调一次 ensure。"""

    def __init__(
        self,
        settings: Settings,
        redis: aioredis.Redis,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._p = AcpProvisioner(settings, redis, transport=transport)

    async def ensure(
        self, thread: Thread, cli: CliSpec, *, model: AcpModel | None = None
    ) -> PodEndpoint:
        ep = await self._p.ensure(thread, cli, model=model)
        return PodEndpoint(url="ws://" + ep.url.removeprefix("http://"), token=ep.token)
