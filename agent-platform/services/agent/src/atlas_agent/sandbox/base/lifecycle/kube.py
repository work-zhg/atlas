"""集群内调 K8s API 的最小客户端：只给模板发布用（读写一个 ConfigMap、给一个 Deployment 打注解）。

★ 不引 kubernetes 官方客户端：只用到四个请求，它带进来的依赖和生成代码远多于这点用处。
★ 凭据用 Pod 的 ServiceAccount：令牌和 CA 在标准挂载路径下。Role 用 resourceNames 只开到
  opensandbox-*-template 这几个 ConfigMap 和对应的 Deployment（deploy/local/30-server.yaml）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

__all__ = ["KubeApi", "KubeError"]

_SA = Path("/var/run/secrets/kubernetes.io/serviceaccount")


class KubeError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class KubeApi:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        verify: str | bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._verify = verify
        self._transport = transport
        self._timeout = timeout_s

    @classmethod
    def in_cluster(cls) -> KubeApi:
        host, port = (
            os.environ.get("KUBERNETES_SERVICE_HOST"),
            os.environ.get("KUBERNETES_SERVICE_PORT"),
        )
        if not host or not port or not (_SA / "token").exists():
            raise KubeError("不在集群里（没有 ServiceAccount 令牌），发布不了沙箱模板")
        return cls(
            f"https://{host}:{port}",
            (_SA / "token").read_text().strip(),
            verify=str(_SA / "ca.crt"),
        )

    async def get(self, path: str) -> dict[str, Any]:
        return await self._request("GET", path)

    async def merge_patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request(
            "PATCH", path, json=body, headers={"Content-Type": "application/merge-patch+json"}
        )

    async def _request(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token}", **kw.pop("headers", {})}
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                verify=self._verify,
                transport=self._transport,
                trust_env=False,
            ) as client:
                resp = await client.request(method, f"{self._base}{path}", headers=headers, **kw)
        except httpx.HTTPError as exc:
            raise KubeError(f"K8s API 不可达：{exc}") from exc
        if resp.status_code >= 400:
            raise KubeError(
                f"K8s API 返回 {resp.status_code}：{resp.text[:300]}", status=resp.status_code
            )
        return resp.json()
