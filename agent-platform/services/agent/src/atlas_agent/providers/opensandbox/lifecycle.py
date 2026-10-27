"""OpenSandbox lifecycle server 的薄客户端（doc/sandbox-opensandbox-design.html §8、§14）。

只用到 lifecycle API 的几个接口：建 / 查 / 列 / 删沙箱、解析端点。

★ 不用官方 SDK（设计 D10）：接口少、版本固定、语义可控；SDK 的执行路径是前台 SSE，
  而 Atlas 的命令必须走后台模式（execd.py）。
★ 每个请求一个 AsyncClient：lifecycle 调用都在 ensure / 回收这种低频路径上，
  连接池带来的收益不抵「进程级共享客户端在多事件循环下」的麻烦。
"""

from __future__ import annotations

import urllib.parse
from typing import Any

import httpx

__all__ = ["API_KEY_HEADER", "LifecycleClient", "LifecycleError"]

API_KEY_HEADER = "OPEN-SANDBOX-API-KEY"


class LifecycleError(RuntimeError):
    """lifecycle server 不可达，或拒绝了请求。消息原样带给模型和用户（配额、镜像这类原因）。"""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class LifecycleClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_s: float = 200.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._timeout = timeout_s
        self._transport = transport

    @property
    def base_url(self) -> str:
        return self._base

    async def create(self, spec: dict[str, Any]) -> dict[str, Any]:
        """建沙箱。server 等到 Running（或失败）才返回。"""
        return await self._json("POST", "/sandboxes", json=spec)

    async def list(self, metadata: dict[str, str]) -> list[dict[str, Any]]:
        """按 metadata 过滤（它会成为 BatchSandbox / Pod 的 label）。"""
        query = urllib.parse.quote(urllib.parse.urlencode(metadata))
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            body = await self._json("GET", f"/sandboxes?metadata={query}&page={page}&pageSize=100")
            items.extend(body.get("items", []))
            info = body.get("pagination") or {}
            if not info.get("hasNextPage"):
                return items
            page += 1

    async def get(self, sandbox_id: str) -> dict[str, Any] | None:
        """None = 已经不存在（删除完成后 GET 返回 404）。"""
        try:
            return await self._json("GET", f"/sandboxes/{sandbox_id}")
        except LifecycleError as exc:
            if exc.status == 404:
                return None
            raise

    async def delete(self, sandbox_id: str) -> None:
        """幂等：已经不存在也算成功。"""
        try:
            await self._call("DELETE", f"/sandboxes/{sandbox_id}")
        except LifecycleError as exc:
            if exc.status != 404:
                raise

    async def endpoint(self, sandbox_id: str, port: int) -> str:
        """沙箱里某个端口的可达地址（集群内直连：Pod IP:端口）。"""
        url, _ = await self.endpoint_with_headers(sandbox_id, port)
        return url

    async def endpoint_with_headers(self, sandbox_id: str, port: int) -> tuple[str, dict[str, str]]:
        """地址 + 访问它必须带的请求头（如 egress 端口的 OPENSANDBOX-EGRESS-AUTH）。"""
        body = await self._json("GET", f"/sandboxes/{sandbox_id}/endpoints/{port}")
        address = str(body["endpoint"])
        url = address if "://" in address else f"http://{address}"
        return url, {str(k): str(v) for k, v in (body.get("headers") or {}).items()}

    # ------------------------------------------------------------------ 内部

    async def _json(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        resp = await self._call(method, path, **kw)
        return resp.json()

    async def _call(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport, trust_env=False
            ) as client:
                resp = await client.request(
                    method, f"{self._base}/v1{path}", headers={API_KEY_HEADER: self._key}, **kw
                )
        except httpx.HTTPError as exc:
            msg = f"OpenSandbox 不可达（{self._base}）：{exc}"
            raise LifecycleError(msg) from exc
        if resp.status_code >= 400:
            raise LifecycleError(
                f"OpenSandbox 返回 {resp.status_code}：{_detail(resp)}", status=resp.status_code
            )
        return resp


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300]
    if isinstance(body, dict):
        return str(body.get("message") or body.get("detail") or body)[:300]
    return str(body)[:300]
