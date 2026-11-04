"""EgressControl：运行期管一个沙箱的出站（写凭据、改策略），直接调沙箱的 egress sidecar。

    EgressLocator        找到沙箱里 egress 的地址和鉴权头。由生命周期领域实现
                         （OpenSandboxRuntime.locate），网络领域因此不依赖生命周期
    OpenSandboxEgress    EgressControl 的 OpenSandbox 实现：/credential-vault、/policy（18080 端口）

★ egress 是普通容器，排在主容器后面：沙箱报 Running 时它可能还没监听，所以写入要重试到它回话。
★ 每次整份覆盖，空列表等于清空：建沙箱的进程崩在「建好」和「写凭据」之间、档案换了令牌、权限被收回，
  都在下一轮补上，不用先读再判断。Vault 的 POST 只能建一次（已存在回 409），覆盖用 PATCH 一次
  原子地增、换、删。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Protocol
from uuid import UUID

import httpx

from ..lifecycle.server import Endpoint
from .policy import NetworkPolicy, policy_json
from .vault import CredentialBinding, vault_body

__all__ = ["EgressControl", "EgressError", "EgressLocator", "OpenSandboxEgress"]

logger = logging.getLogger(__name__)


class EgressError(RuntimeError):
    """egress 没有就绪，或拒绝了写入。"""


class EgressLocator(Protocol):
    async def locate(self, sandbox_uuid: UUID) -> Endpoint: ...


class EgressControl(Protocol):
    async def bind(self, sandbox_uuid: UUID, bindings: list[CredentialBinding]) -> None: ...
    async def update_policy(self, sandbox_uuid: UUID, policy: NetworkPolicy) -> None: ...


class OpenSandboxEgress:
    def __init__(
        self,
        locator: EgressLocator,
        *,
        ready_timeout_s: float = 120.0,
        poll_s: float = 0.5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._locator = locator
        self._ready_timeout = ready_timeout_s
        self._poll = poll_s
        self._transport = transport

    async def bind(self, sandbox_uuid: UUID, bindings: list[CredentialBinding]) -> None:
        """整份覆盖 Credential Vault 里的绑定；空列表等于清空。"""
        ep = await self._locator.locate(sandbox_uuid)
        async with self._client(ep) as client:
            if not bindings:
                await self._until_listening(
                    sandbox_uuid, lambda: client.delete("/credential-vault"), ok=(204, 404)
                )
                return
            body = vault_body(bindings)
            resp = await self._until_listening(
                sandbox_uuid, lambda: client.post("/credential-vault", json=body), ok=(201, 409)
            )
            if resp.status_code == 409:  # 已经有了：一次 PATCH 换成这一份
                current = (await client.get("/credential-vault")).json() or {}
                patch = _replacement(current, body)
                resp = await client.patch("/credential-vault", json=patch)
                if resp.status_code >= 300:
                    raise EgressError(
                        f"改写 Credential Vault 失败：{resp.status_code} {resp.text[:200]}"
                    )
        logger.info(
            "沙箱 %s 写入凭据：%s",
            sandbox_uuid,
            ", ".join(f"{b.name}→{'/'.join(b.hosts)}" for b in bindings),
        )

    async def update_policy(self, sandbox_uuid: UUID, policy: NetworkPolicy) -> None:
        """整表换成新策略。egress 重建 nft 规则，DNS 学到的放行 IP 会清空，已建立的连接不断。

        ★ 未接入：现在没有调用方。凭据注入只能在建沙箱时开，这里改不了 inject_credentials。
        """
        ep = await self._locator.locate(sandbox_uuid)
        body = policy_json(policy)["networkPolicy"]
        async with self._client(ep) as client:
            await self._until_listening(
                sandbox_uuid, lambda: client.put("/policy", json=body), ok=(200, 204)
            )

    def _client(self, ep: Endpoint) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=ep.url.rstrip("/"),
            headers=dict(ep.headers),
            timeout=10.0,
            transport=self._transport,
            trust_env=False,
        )

    async def _until_listening(
        self, sandbox_uuid: UUID, call: Any, *, ok: tuple[int, ...]
    ) -> httpx.Response:
        """egress 晚于主容器起来：连不上、或回 412（Vault 还没就绪）就重试到时限。"""
        deadline = time.monotonic() + self._ready_timeout
        while True:
            try:
                resp: httpx.Response = await call()
            except httpx.TransportError:
                resp = None  # type: ignore[assignment]
            if resp is not None:
                if resp.status_code in ok:
                    return resp
                if resp.status_code != 412:
                    raise EgressError(f"egress 拒绝了请求：{resp.status_code} {resp.text[:200]}")
            if time.monotonic() > deadline:
                raise EgressError(f"沙箱 {sandbox_uuid} 的 egress 在时限内没有就绪")
            await asyncio.sleep(self._poll)


def _replacement(current: dict[str, Any], desired: dict[str, Any]) -> dict[str, Any]:
    """现状 → 目标的一次 PATCH：同名的换、新的加、多的删。"""

    def names(items: list[dict[str, Any]]) -> set[str]:
        return {str(i.get("name")) for i in items or []}

    patch: dict[str, Any] = {}
    for key in ("credentials", "bindings"):
        have, want = names(current.get(key, [])), {i["name"]: i for i in desired[key]}
        section = {
            "replace": [v for n, v in want.items() if n in have],
            "add": [v for n, v in want.items() if n not in have],
            "delete": sorted(have - set(want)),
        }
        patch[key] = {k: v for k, v in section.items() if v}
    if isinstance(current.get("revision"), int):
        patch["expectedRevision"] = current["revision"]
    return patch
