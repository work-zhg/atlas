"""系统接入（c4-model/opscenter/code-gateway.html）：运营中心碰各业务系统的唯一通道。

    SystemRegistry   有哪些系统、地址、声明了哪几组能力（部署配置，不进库）
    OpsClient        一个系统的运营接口客户端：每次调用带上运营人员和幂等键，用这个系统的
                     运营密钥签名，统一超时；失败归成可以重做（连不上、超时、5xx）和被拒绝
    proxy_allowed    运营控制台经「系统代理」直接调的接口白名单（应用目录、容量、系统参数、
                     系统任务、运行环境、技能市场、记忆作用域）。建租户、创建、删除、挡住入口
                     这些不在里面：只能经租户操作（tenants.py）按顺序做

★ 契约里没有读租户内容的接口 —— 「运营中心看不到租户内容」由各系统的运营接口保证，这里只是通道。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

import httpx
from atlas_opsapi import OperatorStamp, sign

from .settings import OpsSettings, SystemConfig

__all__ = [
    "CallFailed",
    "Capability",
    "OpsClient",
    "OpsReply",
    "SystemEntry",
    "SystemRegistry",
    "owner_only",
    "proxy_allowed",
    "stamp_of",
]

PREFIX = "/ops/v1"


class Capability(StrEnum):
    #: 用户中心专有：租户目录、应用目录
    USER_CENTER = "user_center"
    #: 存租户数据的系统必选：租户的创建、删除（业务系统不知道租户状态，没有挂起、恢复）
    LIFECYCLE = "lifecycle"
    #: 管共用设施的：份额和用量
    CAPACITY = "capacity"
    #: 系统参数（应用配置库）
    SETTINGS = "settings"
    #: 系统任务（定时任务库）
    JOBS = "jobs"
    #: 运行环境：沙箱模板（只有 Atlas）
    RUNTIME_ENV = "runtime_env"
    #: 技能市场（只有技能应用）
    SKILL_MARKET = "skill_market"
    #: 记忆作用域（产品自带的定义，只有记忆服务）
    MEMORY_SCOPES = "memory_scopes"


@dataclass(frozen=True)
class SystemEntry:
    code: str
    name: str
    base_url: str
    capabilities: frozenset[Capability]
    #: 这个系统在运营中心里那个模块的数据编码（用户中心里授它的「全部」级别，access.py）
    module_code: str

    def has(self, cap: Capability) -> bool:
        return cap in self.capabilities

    def view(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "name": self.name,
            "capabilities": sorted(c.value for c in self.capabilities),
            "module": self.module_code,
        }


class SystemRegistry:
    def __init__(self, systems: list[SystemConfig]) -> None:
        entries = []
        for s in systems:
            caps = frozenset(Capability(c) for c in s.capabilities)
            entries.append(SystemEntry(s.code, s.name, s.base_url.rstrip("/"), caps, s.module_code))
        codes = [e.code for e in entries]
        if len(set(codes)) != len(codes):
            raise ValueError(f"系统登记里有重复的编码：{codes}")
        if sum(e.has(Capability.USER_CENTER) for e in entries) != 1:
            raise ValueError("系统登记里要有且只有一个用户中心（能力 user_center）")
        self._entries = entries

    def all(self) -> list[SystemEntry]:
        return list(self._entries)

    def get(self, code: str) -> SystemEntry | None:
        return next((e for e in self._entries if e.code == code), None)

    def with_capability(self, cap: Capability) -> list[SystemEntry]:
        """按登记顺序（挂起、删除时依次调）。"""
        return [e for e in self._entries if e.has(cap)]

    @property
    def user_center(self) -> SystemEntry:
        return self.with_capability(Capability.USER_CENTER)[0]


@dataclass(frozen=True)
class OpsReply:
    status: int
    body: bytes
    content_type: str

    def json(self) -> Any:
        return json.loads(self.body or b"null")


class CallFailed(Exception):
    """调一个系统失败。retryable：连不上、超时、5xx，可以重做；否则是对方明确拒绝，要人看。"""

    def __init__(self, system: str, message: str, *, retryable: bool, status: int | None = None):
        super().__init__(f"{system}：{message}")
        self.system = system
        self.message = message
        self.retryable = retryable
        self.status = status


class OpsClient:
    """调各系统的运营接口。一个进程一个，按系统复用连接。"""

    def __init__(
        self,
        registry: SystemRegistry,
        settings: OpsSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.registry = registry
        self._keys = {k: v.encode() for k, v in settings.system_keys.items()}
        self._http = httpx.AsyncClient(
            timeout=settings.call_timeout_s, transport=transport, trust_env=False
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def request(
        self,
        system: str,
        method: str,
        path: str,
        *,
        body: bytes = b"",
        content_type: str | None = "application/json",
        params: dict[str, Any] | None = None,
        stamp: OperatorStamp,
    ) -> OpsReply:
        """原样转发：path 是 /ops/v1 之后的部分（如 /tenants/xxx/suspend）。不判状态码。"""
        entry = self.registry.get(system)
        if entry is None:
            raise CallFailed(system, "没有登记这个系统", retryable=False)
        key = self._keys.get(system)
        if not key:
            raise CallFailed(system, "没有配置这个系统的运营密钥", retryable=False)
        if not sent_as_is(path):
            # 会被 httpx 改写，签名发出去的就不是调用方给的那个路径了
            raise CallFailed(system, "路径里不能有点段、? 或 #", retryable=False)
        url = httpx.URL(f"{entry.base_url}{PREFIX}{path}", params=params)
        # 运营头（操作人、幂等键）随签名一起签，见 atlas_opsapi.signature
        # ★ 签服务端验的那个：OpsGuard 验的是解码后的路径（request.url.path）加原样的查询串。
        #   签编码后的 raw_path 的话，路径里有中文、空格时两边对不上，一律 401
        signed = url.path + (f"?{url.query.decode()}" if url.query else "")
        headers = sign(key, method, signed, body, stamp=stamp)
        if body and content_type:
            headers["Content-Type"] = content_type
        try:
            resp = await self._http.request(method, url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise CallFailed(system, f"连不上（{type(exc).__name__}）", retryable=True) from exc
        return OpsReply(resp.status_code, resp.content, resp.headers.get("content-type", ""))

    async def call(
        self,
        system: str,
        method: str,
        path: str,
        payload: Any = None,
        *,
        stamp: OperatorStamp,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """JSON 调用：2xx 返回解析后的响应体；其余抛 CallFailed（5xx 可重做，4xx 是拒绝）。"""
        body = b"" if payload is None else json.dumps(payload, default=str).encode()
        reply = await self.request(system, method, path, body=body, params=params, stamp=stamp)
        if 200 <= reply.status < 300:
            return reply.json() if reply.body else None
        raise CallFailed(
            system,
            _message(reply),
            retryable=reply.status >= 500 or reply.status == 429,
            status=reply.status,
        )


def _message(reply: OpsReply) -> str:
    try:
        data = reply.json()
    except ValueError:
        return f"HTTP {reply.status}"
    if isinstance(data, dict):
        for k in ("message", "detail"):
            if isinstance(data.get(k), str):
                return f"HTTP {reply.status}：{data[k]}"
        err = data.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            return f"HTTP {reply.status}：{err['message']}"
    return f"HTTP {reply.status}"


def stamp_of(
    operator_uuid: UUID | None,
    name: str | None,
    *,
    operation: UUID | None = None,
    idempotency_key: UUID | None = None,
) -> OperatorStamp:
    return OperatorStamp(
        operator_uuid=operator_uuid,
        operator_name=name or "",
        operation_uuid=operation,
        idempotency_key=idempotency_key,
    )


# ───────────────────────────── 系统代理的白名单

_ANY = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_READ = frozenset({"GET"})
_UUID = r"[0-9a-fA-F-]{36}"

#: 能力 → [(方法, 路径正则)]，路径是 /ops/v1 之后的部分
_RULES: dict[Capability, list[tuple[frozenset[str], re.Pattern[str]]]] = {
    Capability.USER_CENTER: [
        # 租户目录：只读，外加改名字 / 名额 / 到期、给租户开通应用。
        # 建、挡住、删、补发密码只经租户操作
        (_READ, re.compile(rf"^/tenants(/expired|/{_UUID}(/apps)?)?$")),
        (frozenset({"PATCH"}), re.compile(rf"^/tenants/{_UUID}$")),
        (frozenset({"PUT"}), re.compile(rf"^/tenants/{_UUID}/apps/{_UUID}$")),
        # 应用目录：应用、凭据、操作码、菜单、数据编码、应用内置角色
        (_ANY, re.compile(r"^/(apps|operations|menus|data-types|roles)(/.*)?$")),
    ],
    Capability.CAPACITY: [
        (_READ, re.compile(rf"^/(capacity|quotas|usage/{_UUID})$")),
        (frozenset({"PUT"}), re.compile(rf"^/quotas/(default|{_UUID})$")),
    ],
    Capability.SETTINGS: [(_ANY, re.compile(r"^/configs(/.*)?$"))],
    Capability.JOBS: [(_ANY, re.compile(r"^/jobs(/.*)?$"))],
    Capability.RUNTIME_ENV: [(_ANY, re.compile(r"^/sandbox-templates(/.*)?$"))],
    Capability.SKILL_MARKET: [(_ANY, re.compile(r"^/market(/.*)?$"))],
    Capability.MEMORY_SCOPES: [(_ANY, re.compile(r"^/scopes(/.*)?$"))],
}


#: 系统代理里不可恢复的操作：要 Owner（其余写操作要读写，见 access.required_level）
_OWNER_RULES: dict[Capability, list[tuple[frozenset[str], re.Pattern[str]]]] = {
    Capability.USER_CENTER: [
        # 轮换应用凭据：接入方会立刻断开
        (frozenset({"POST"}), re.compile(rf"^/apps/{_UUID}/rotate-secret$")),
        # 删目录里的东西：删数据编码、角色会连带删掉授给人的权限
        (frozenset({"DELETE"}), re.compile(r"^/(operations|menus|data-types|roles)/.+$")),
    ],
}


def owner_only(entry: SystemEntry, method: str, path: str) -> bool:
    method = method.upper()
    return any(
        method in methods and pattern.match(path)
        for cap in entry.capabilities
        for methods, pattern in _OWNER_RULES.get(cap, [])
    )


def sent_as_is(path: str) -> bool:
    """httpx 会不会原样发出这个路径（path 是解码后的，进路由时 Starlette 已解过一次）。

    ★ httpx 构造 URL 时会改写路径：规整点段（/apps/../tenants/x → /tenants/x），把 ? # 之后的
      当成查询串和片段（/apps/..%23 解码成 /apps/..#，发出去的是 /ops/v1），把 %2f 当成 /。
      白名单按原样匹配、发出去的却是改写后的路径，就能带着运营签名直达白名单外的接口。
      所以只认一条：发出去的路径就是匹配过的那个，改写了的一律不转发（不逐个列举危险字符）。
    """
    try:
        url = httpx.URL(f"http://ops{path}")
    except httpx.InvalidURL:  # 换行等控制字符
        return False
    return url.path == path and not url.query and not url.fragment


def proxy_allowed(entry: SystemEntry, method: str, path: str) -> bool:
    """★ 会被改写的路径一律不放行（见 sent_as_is）：白名单只对原样发出的路径有意义。"""
    if not sent_as_is(path):
        return False
    method = method.upper()
    for cap in entry.capabilities:
        for methods, pattern in _RULES.get(cap, []):
            if method in methods and pattern.match(path):
                return True
    return False
