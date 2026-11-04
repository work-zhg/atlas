"""一个 OpenSandbox lifecycle server 的 HTTP 客户端和翻译（防腐层）。

CreateSpec → 创建请求体（secret_env 和令牌并进 env、timeout 写 null、PVC 不自动建），返回的 JSON
→ 状态和 id；404 在查询里翻成「不在」、在删除里算成功；列表自动翻页。

★ 不用官方 SDK：接口少、版本固定、语义可控；SDK 的执行路径是前台 SSE，而命令必须走后台模式。
★ 每个请求一个 AsyncClient：lifecycle 调用都在建、回收这种低频路径上，连接池的收益不抵「进程级
  共享客户端在多事件循环下」的麻烦。
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from pydantic import SecretStr

from ..network.policy import policy_json
from .spec import CreateSpec
from .state import SandboxState

__all__ = [
    "API_KEY_HEADER",
    "EXECD_TOKEN_ENV",
    "Endpoint",
    "LifecycleError",
    "LifecycleServer",
    "RemoteSandbox",
]

API_KEY_HEADER = "OPEN-SANDBOX-API-KEY"
#: execd 读访问令牌的环境变量（每个沙箱都会注入 execd，不设令牌就是无鉴权的命令执行入口）
EXECD_TOKEN_ENV = "EXECD_ACCESS_TOKEN"


class LifecycleError(RuntimeError):
    """lifecycle server 不可达，或拒绝了请求。消息原样带给模型和用户（配额、镜像这类原因）。"""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Endpoint:
    """沙箱里某个端口的地址，加访问它必须带的请求头（如 egress 的 OPENSANDBOX-EGRESS-AUTH）。

    不含令牌。
    """

    url: str
    headers: Mapping[str, str]


@dataclass(frozen=True)
class RemoteSandbox:
    """OpenSandbox 上的一个沙箱，按它报的样子。"""

    external_id: str
    state: SandboxState
    labels: Mapping[str, str]
    created_at: datetime | None
    message: str = ""


class LifecycleServer:
    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: SecretStr,
        *,
        timeout_s: float = 200.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = name
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._timeout = timeout_s
        self._transport = transport

    @property
    def base_url(self) -> str:
        return self._base

    # ------------------------------------------------------------------ 沙箱

    async def create(
        self, spec: CreateSpec, *, access_token: SecretStr, service_token: SecretStr | None
    ) -> RemoteSandbox:
        """建沙箱。server 等到可用（或失败）才返回。

        所以超时要盖过它的 sandbox_create_timeout_seconds。
        """
        body = await self._json(
            "POST", "/sandboxes", json=request_body(spec, access_token, service_token)
        )
        return _remote(body)

    async def get(self, external_id: str) -> RemoteSandbox | None:
        """None = 已经不在（删除完成后 GET 回 404）。"""
        try:
            return _remote(await self._json("GET", f"/sandboxes/{external_id}"))
        except LifecycleError as exc:
            if exc.status == 404:
                return None
            raise

    async def list(self, labels: Mapping[str, str]) -> list[RemoteSandbox]:
        """按标签过滤（它们就是 BatchSandbox / Pod 的 label），自动翻页。"""
        query = urllib.parse.quote(urllib.parse.urlencode(dict(labels)))
        items: list[RemoteSandbox] = []
        page = 1
        while True:
            body = await self._json("GET", f"/sandboxes?metadata={query}&page={page}&pageSize=100")
            items.extend(_remote(sb) for sb in body.get("items", []))
            if not (body.get("pagination") or {}).get("hasNextPage"):
                return items
            page += 1

    async def delete(self, external_id: str) -> None:
        """幂等：已经不在也算成功。"""
        try:
            await self._call("DELETE", f"/sandboxes/{external_id}")
        except LifecycleError as exc:
            if exc.status != 404:
                raise

    async def endpoint(self, external_id: str, port: int) -> Endpoint:
        """集群内直连的地址（Pod IP:端口）和访问它要带的请求头。"""
        body = await self._json("GET", f"/sandboxes/{external_id}/endpoints/{port}")
        address = str(body["endpoint"])
        url = address if "://" in address else f"http://{address}"
        return Endpoint(url, {str(k): str(v) for k, v in (body.get("headers") or {}).items()})

    # ------------------------------------------------------------------ 内部

    async def _json(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        return (await self._call(method, path, **kw)).json()

    async def _call(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport, trust_env=False
            ) as client:
                resp = await client.request(
                    method,
                    f"{self._base}/v1{path}",
                    headers={API_KEY_HEADER: self._key.get_secret_value()},
                    **kw,
                )
        except httpx.HTTPError as exc:
            raise LifecycleError(f"OpenSandbox 不可达（{self.name}：{self._base}）：{exc}") from exc
        if resp.status_code >= 400:
            raise LifecycleError(
                f"OpenSandbox 返回 {resp.status_code}：{_detail(resp)}", status=resp.status_code
            )
        return resp


def request_body(
    spec: CreateSpec, access_token: SecretStr, service_token: SecretStr | None
) -> dict[str, Any]:
    """CreateSpec + 令牌 → POST /v1/sandboxes 的请求体。"""
    env = dict(spec.env)
    env.update({k: v.get_secret_value() for k, v in spec.secret_env.items()})
    env[EXECD_TOKEN_ENV] = access_token.get_secret_value()
    if spec.service_token_env:
        if service_token is None:
            raise ValueError("声明了 service_token_env 却没有服务令牌")
        env[spec.service_token_env] = service_token.get_secret_value()
    r = spec.resources
    return {
        "image": {"uri": spec.image},
        "entrypoint": list(spec.entrypoint),
        "timeout": None,  # 不自动过期：回收由 Atlas 做
        "resourceLimits": {"cpu": r.cpu_limit, "memory": r.memory_limit},
        "resourceRequests": {"cpu": r.cpu_request, "memory": r.memory_request},
        "env": env,
        "metadata": spec.labels.as_dict(),
        "volumes": [
            {
                "name": v.name,
                "pvc": {"claimName": v.claim, "createIfNotExists": False},
                "mountPath": v.mount_path,
                "subPath": v.sub_path,
                "readOnly": v.read_only,
            }
            for v in spec.volumes
        ],
        **policy_json(spec.network),
    }


def _remote(body: dict[str, Any]) -> RemoteSandbox:
    status = body.get("status") or {}
    return RemoteSandbox(
        external_id=str(body["id"]),
        state=SandboxState.parse(status.get("state")),
        labels={str(k): str(v) for k, v in (body.get("metadata") or {}).items()},
        created_at=_parse_time(body.get("createdAt")),
        message=str(status.get("message") or ""),
    )


def _parse_time(raw: object) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300]
    if isinstance(body, dict):
        return str(body.get("message") or body.get("detail") or body)[:300]
    return str(body)[:300]
