"""镜像里的程序（假）与外部世界：ACP 镜像的 bridge + CLI，代码托管平台 Gitee。

ACP 镜像 = bridge + adapter + CLI 三件套（真实的协议见 ACP 详设）。demo 只留与迁移有关的部分：

    令牌     从环境变量 ATLAS_BRIDGE_TOKEN 读（原来读 per-Pod Secret 文件；设计 §11.2 的改动 1）
    模型 key 按 ATLAS_BRIDGE_AGENT_ENV_FILES 读文件，交给 adapter 进程的环境（改动 2）——
             key 只以文件形式挂在 ACP 命名空间里，不出现在 Pod spec / CR 里
    git     仓库地址不带凭据；push 的认证由 egress 的 Credential Vault 注入（改动 3）
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from tracing import say

BRIDGE_PORT = 8900
GITEE_REPO = "https://gitee.com/atlas/demo.git/git-receive-pack"


class FakeBridge:
    def __init__(
        self,
        pod: str,
        env: dict[str, str],
        resolve: Callable[[str], Path],
        egress: Any,
        *,
        on_close: Callable[[], None],
    ) -> None:
        self.pod = pod
        self._token = env["ATLAS_BRIDGE_TOKEN"]
        self._session = env["ATLAS_BRIDGE_SESSION_ID"]
        # adapter 进程的环境：白名单里的普通变量 + 从文件读出来的凭据
        allow = env["ATLAS_BRIDGE_AGENT_ENV_ALLOW"].split(",")
        self.agent_env = {k: v for k, v in env.items() if k in allow}
        for item in filter(None, env.get("ATLAS_BRIDGE_AGENT_ENV_FILES", "").split(",")):
            name, path = item.split("=", 1)
            self.agent_env[name] = resolve(path).read_text()
        self._cli = FakeCli(pod, self.agent_env, resolve("/workspace"), egress, on_close)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/readyz":
            return httpx.Response(200)
        if request.headers.get("authorization") != f"Bearer {self._token}":
            say("bridge", f"{self.pod} 令牌不对 → 401")
            return httpx.Response(401)
        if (
            request.headers.get("x-atlas-session") != self._session
        ):  # 会话绑定：令牌对、会话不对也拒
            return httpx.Response(403)
        prompt = json.loads(request.content)["text"]
        reply = await self._cli.run(prompt)
        return httpx.Response(200, json={"reply": reply})


class FakeCli:
    """CLI（如 claude-code）：直接在挂载的 /workspace 里写文件，用 git 推到代码托管。"""

    def __init__(
        self,
        pod: str,
        env: dict[str, str],
        workspace: Path,
        egress: Any,
        on_close: Callable[[], None],
    ) -> None:
        self.pod, self.env, self.workspace, self._egress, self._on_close = (
            pod,
            env,
            workspace,
            egress,
            on_close,
        )

    async def run(self, prompt: str) -> str:
        has_key = "ANTHROPIC_AUTH_TOKEN" in self.env
        git_secrets = [k for k in self.env if "GIT" in k and ("TOKEN" in k or "PASS" in k)]
        say(
            "CLI",
            f"{self.pod} 收到「{prompt}」；环境里有模型 key：{has_key}，"
            f"git 凭据：{git_secrets or '没有'}",
        )
        (self.workspace / "RELEASE.md").write_text("# v1.2\n\n- 修复登录超时\n- 新增导出\n")
        self._on_close()  # close → 写穿
        say("CLI", f"{self.pod} 写了 /workspace/RELEASE.md；git push（仓库地址不带凭据）")
        if self._egress is None:
            return "git push 失败：没有出站代理（创建时没开 credentialProxy）"
        resp = await self._egress.request("POST", GITEE_REPO)
        return (
            "RELEASE.md 已写好并推送"
            if resp.status_code == 200
            else f"git push 失败：Gitee 返回 {resp.status_code}"
        )


class FakeGitee:
    """代码托管平台：只认对的 Basic 认证。"""

    def __init__(self, username: str, token: str) -> None:
        raw = f"{username}:{token}".encode()
        self._expected = "Basic " + base64.b64encode(raw).decode()

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") == self._expected:
            say("Gitee", "收到 push，认证通过")
            return httpx.Response(200)
        say("Gitee", "收到 push，没有认证 → 401")
        return httpx.Response(401)
