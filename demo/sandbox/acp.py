"""server 侧：ACP 智能体 —— PodProvider 的 OpenSandbox 实现（设计 §11、§12）。

    OpenSandboxPods.ensure(thread)   替代 ClusterPods：经 opensandbox-acp 建 / 复用 ACP 的 Pod
                                     git 档案先授权（原 cluster 的 allowed_apps 判定搬到这里），
                                     建好后把凭据写进 Credential Vault —— 凭据只在 egress 的内存里
    AcpRuntime.run_turn              HostRuntime 的极简版：等 bridge 就绪 → 带令牌和会话头发一轮

★ HostRuntime 只认 PodProvider 这个接缝（ensure → 地址 + 令牌），换成 OpenSandbox 它不用改。
★ 模型 key 不经过这里：它是 ACP 实例模板里挂的静态 Secret 文件，bridge 自己读（images.py）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
from provision import ACP, BRIDGE_PORT, Endpoint, Ensurer, acp_spec
from tracing import say

ACP_IMAGE = "atlas-acp:dev"
#: git 凭据档案（从 cluster 配置搬到 agent；真实的凭据放 Secret）
GIT_PROFILES = {
    "teamflow-gitee": {
        "allowed_apps": ["teamflow"],
        "host": "gitee.com",
        "username": "x-access-token",
        "token": "gitee-token-demo",
    }
}


class GitProfileDenied(RuntimeError):
    pass


@dataclass
class AcpThread:
    id: str
    workspace_id: str  # 子智能体是父会话的 id
    user_id: str
    source_app: str = ""
    git_profile: str = ""
    adapter: str = "claude-code-acp"


class OpenSandboxPods:
    def __init__(self, ensurer: Ensurer) -> None:
        self._e = ensurer

    async def ensure(self, thread: AcpThread) -> Endpoint:
        profile = self._authorize(thread)

        async def write_vault(sandbox_id: str) -> None:
            if profile is None:
                return
            await self._e.servers[ACP].credential_vault(
                sandbox_id,
                {
                    "credentials": [
                        {"name": "git", "username": profile["username"], "secret": profile["token"]}
                    ],
                    "bindings": [{"host": profile["host"], "credential": "git", "auth": "basic"}],
                },
            )
            say(
                "ACP",
                f"{sandbox_id} 写入 Credential Vault："
                f"{profile['host']} → 档案 {thread.git_profile}",
            )

        return await self._e.ensure(
            ACP,
            thread=thread.id,
            user=thread.user_id,
            image=ACP_IMAGE,
            spec=lambda token: acp_spec(
                thread=thread.id,
                ws=thread.workspace_id,
                user=thread.user_id,
                token=token,
                image=ACP_IMAGE,
                adapter=thread.adapter,
                git=profile is not None,
            ),
            port=BRIDGE_PORT,
            after_create=write_vault,
        )

    def _authorize(self, thread: AcpThread) -> dict | None:
        """★ 档案是凭据：谁能用由 Atlas 判定，名字写错或无权一律拒绝，而不是静默不给。"""
        if not thread.git_profile:
            return None
        profile = GIT_PROFILES.get(thread.git_profile)
        if profile is None:
            raise GitProfileDenied(f"git 凭据档案 {thread.git_profile!r} 不存在")
        if thread.source_app not in profile["allowed_apps"]:
            raise GitProfileDenied(
                f"只有 {profile['allowed_apps']} 建的会话能用 {thread.git_profile!r}"
            )
        return profile


class AcpRuntime:
    def __init__(self, pods: OpenSandboxPods, transport: httpx.AsyncBaseTransport) -> None:
        self._pods, self._transport = pods, transport

    async def run_turn(self, thread: AcpThread, prompt: str) -> str:
        ep = await self._pods.ensure(thread)
        async with httpx.AsyncClient(transport=self._transport) as client:
            for _ in range(
                50
            ):  # 等 bridge 就绪（server 只等到 Pod Running，应用层的就绪由 Atlas 等）
                if (await client.get(f"{ep.url}/readyz")).status_code == 200:
                    break
                await asyncio.sleep(0.02)
            headers = {"authorization": f"Bearer {ep.token}", "x-atlas-session": thread.id}
            resp = await client.post(f"{ep.url}/prompt", json={"text": prompt}, headers=headers)
        resp.raise_for_status()
        return resp.json()["reply"]
