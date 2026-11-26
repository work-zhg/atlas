"""路由表的领域模型（c4-model/apigateway/code-routing.html）：站点、服务路由、认证模式、转发档位。

routes.yaml 是唯一来源：每个对外的后端或前端只登记名字、种类、子路径、认证模式和档位，不写地址。
外部路径第一段 = 名字 = 内部域名第一段：后端 /<名>/… 去掉 /<名> 发往 <名>.internal…，
前端 /<名>-ui/… 原样发往 <名>-ui.internal…。

★ 环境相关的值（域名、内部 Ingress 的地址、白名单网段）在 routes.yaml 里写成 ${VAR} / ${VAR:-默认}，
  加载时从环境变量取；没给又没默认值的一次列全再报错。host 取值为空的站点不渲染（可选站点）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

#: 禁区段：路径里出现「/段/」就 404（各系统的内网接口、运营接口、用户中心的开放接口）
FORBIDDEN_SEGMENTS = ("internal", "ops", "open")


class Kind(StrEnum):
    API = "api"
    UI = "ui"


class AuthMode(StrEnum):
    #: 验 JWT 并写身份头
    AUTH = "auth"
    #: 不验，原样转给用户中心（登录、刷新、登出）
    PASSTHROUGH = "passthrough"
    #: 不验，凭据（专属 Key、设备令牌）由后面的子网关自己认
    SELF_AUTH = "self_auth"
    #: 不验（页面、静态资源、带令牌的预览站点）
    PUBLIC = "public"


class ProxyProfile(StrEnum):
    DEFAULT = "DEFAULT"
    SSE = "SSE"
    WEBSOCKET = "WEBSOCKET"
    UPLOAD = "UPLOAD"
    STREAM = "STREAM"


class SiteKind(StrEnum):
    #: 走 Atlas 的完整规则
    ATLAS = "atlas"
    #: 只清身份头，整域转给 <name>.internal…
    PASSTHROUGH = "passthrough"


class ConfigError(ValueError):
    """routes.yaml 读不进来（格式、缺环境变量）。硬规则不通过是 rules.Problem，不是它。"""


@dataclass(frozen=True)
class Route:
    """一条对外路由：服务里认证模式或档位相同的一段路径。"""

    service: str
    kind: Kind
    #: 服务内的路径：/ 是整个服务；以 / 结尾是前缀；否则精确匹配。* 只能占一整段，匹配一段
    subpath: str
    mode: AuthMode
    proxy: ProxyProfile
    #: 改密前能否使用（mcp=true 的令牌只放行这些路由）
    pending_password_ok: bool = False
    #: 登录接口：回 401 计入这个 IP 的登录失败
    login: bool = False
    #: 预览站点：路径里带令牌，可以 public，访问日志不记路径
    preview: bool = False
    #: 过渡期保留的旧外部路径（完整前缀，如 /v1/previews/），转到同一个上游路径
    legacy_paths: tuple[str, ...] = ()

    @property
    def id(self) -> str:
        return f"{self.service}:{self.subpath}"

    @property
    def is_glob(self) -> bool:
        return "*" in self.subpath

    @property
    def is_prefix(self) -> bool:
        return self.subpath.endswith("/")


@dataclass(frozen=True)
class Service:
    name: str
    kind: Kind
    routes: tuple[Route, ...]
    #: 过渡期保留的旧名字（如 atlas-api）：每条路由在旧名字下也生成一份，去掉的是旧名字
    legacy_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class Site:
    host: str
    kind: SiteKind
    #: 透传站点拼内部域名用（langfuse → langfuse.internal…）
    name: str | None = None


@dataclass(frozen=True)
class Redirect:
    """旧页面地址 → 新前端：/atlas/… 301 到 /atlas-ui/…（只剩没被路由占用的部分）。"""

    source: str
    target: str


@dataclass(frozen=True)
class JwtSettings:
    issuer: str = "atlas-usercenter"
    audience: str = "atlas-gateway"
    clock_skew: int = 30
    max_len: int = 8192
    access_cookie: str = "uc_access"
    csrf_cookie: str = "uc_csrf"
    csrf_header: str = "X-UC-CSRF"


@dataclass(frozen=True)
class JwksSettings:
    #: 公钥集在哪个服务上（经内部 Ingress，Host = <service>.internal…）
    service: str = "uc"
    path: str = "/api/v1/auth/jwks"
    cache: str = "/var/lib/atlas-gateway/jwks.json"
    refresh: int = 300
    retry: int = 5
    kid_refetch_gap: int = 10
    timeout_ms: int = 3000


@dataclass(frozen=True)
class BanSettings:
    window: int = 600
    threshold: int = 20
    duration: int = 1800
    allow: tuple[str, ...] = ()


@dataclass(frozen=True)
class Settings:
    #: 内部 Ingress 的固定地址（IP:端口）——网关唯一的上游
    internal_ingress: str
    internal_suffix: str = "internal.techstz.com"
    root_redirect: str = "/atlas-ui"
    cert_dir: str = "/etc/letsencrypt/live"
    acme_root: str = "/var/www/acme"
    log_dir: str = "/var/log/atlas-gateway"
    admin_listen: str = "127.0.0.1:8081"
    https_port: int = 443
    http_port: int = 80
    jwt: JwtSettings = field(default_factory=JwtSettings)
    jwks: JwksSettings = field(default_factory=JwksSettings)
    ban: BanSettings = field(default_factory=BanSettings)

    @property
    def ingress_host(self) -> str:
        return self.internal_ingress.rsplit(":", 1)[0]

    @property
    def ingress_port(self) -> int:
        return int(self.internal_ingress.rsplit(":", 1)[1])

    def internal_host(self, name: str) -> str:
        return f"{name}.{self.internal_suffix}"


@dataclass(frozen=True)
class RouteTable:
    settings: Settings
    sites: tuple[Site, ...]
    services: tuple[Service, ...]
    redirects: tuple[Redirect, ...] = ()

    def routes(self) -> list[Route]:
        return [r for s in self.services for r in s.routes]

    def service(self, name: str) -> Service | None:
        return next((s for s in self.services if s.name == name), None)


# ─────────────────────────────────────────────── 加载

_VAR = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")


def _substitute(value: Any, env: dict[str, str], missing: set[str]) -> Any:
    if isinstance(value, str):

        def repl(m: re.Match[str]) -> str:
            name, default = m.group(1), m.group(2)
            if name in env:
                return env[name]
            if default is not None:
                return default
            missing.add(name)
            return ""

        return _VAR.sub(repl, value)
    if isinstance(value, list):
        return [_substitute(v, env, missing) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, env, missing) for k, v in value.items()}
    return value


def _section(raw: dict[str, Any], key: str, cls: type) -> Any:
    data = raw.get(key) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"gateway.{key} 应该是映射")
    known = set(cls.__dataclass_fields__)
    extra = set(data) - known
    if extra:
        raise ConfigError(f"gateway.{key} 有不认识的字段：{sorted(extra)}")
    if key == "ban" and "allow" in data:
        data = {**data, "allow": tuple(_split_list(data["allow"]))}
    # 来自环境变量的数字是字符串：按字段的默认值类型转
    defaults = cls()
    out: dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(getattr(defaults, k), int) and isinstance(v, str):
            try:
                v = int(v)
            except ValueError as exc:
                raise ConfigError(f"gateway.{key}.{k} 要是整数：{v!r}") from exc
        out[k] = v
    return cls(**out)


def _split_list(value: Any) -> list[str]:
    """白名单可以写成列表，也可以是逗号分隔的一个字符串（来自环境变量）。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


def _route(service: str, kind: Kind, raw: dict[str, Any]) -> Route:
    allowed = {
        "subpath",
        "mode",
        "proxy",
        "pending_password_ok",
        "login",
        "preview",
        "legacy_paths",
    }
    extra = set(raw) - allowed
    if extra:
        raise ConfigError(f"服务 {service} 的路由有不认识的字段：{sorted(extra)}")
    default_mode = AuthMode.AUTH if kind is Kind.API else AuthMode.PUBLIC
    try:
        return Route(
            service=service,
            kind=kind,
            subpath=str(raw.get("subpath", "/")),
            mode=AuthMode(raw.get("mode", default_mode)),
            proxy=ProxyProfile(raw.get("proxy", ProxyProfile.DEFAULT)),
            pending_password_ok=bool(raw.get("pending_password_ok", False)),
            login=bool(raw.get("login", False)),
            preview=bool(raw.get("preview", False)),
            legacy_paths=tuple(raw.get("legacy_paths") or ()),
        )
    except ValueError as exc:
        raise ConfigError(f"服务 {service}：{exc}") from exc


def _service(raw: dict[str, Any]) -> Service:
    allowed = {"name", "kind", "routes", "legacy_names", "mode", "proxy"}
    extra = set(raw) - allowed
    if extra:
        raise ConfigError(f"服务 {raw.get('name')} 有不认识的字段：{sorted(extra)}")
    name = str(raw["name"])
    try:
        kind = Kind(raw["kind"])
    except (KeyError, ValueError) as exc:
        raise ConfigError(f"服务 {name} 的 kind 只能是 api 或 ui") from exc
    # 只有一条整服务路由时，可以把 mode / proxy 直接写在服务上
    base = {k: raw[k] for k in ("mode", "proxy") if k in raw}
    routes_raw = raw.get("routes") or [{}]
    routes = tuple(_route(name, kind, {**base, **r}) for r in routes_raw)
    return Service(
        name=name, kind=kind, routes=routes, legacy_names=tuple(raw.get("legacy_names") or ())
    )


def load(path: str | Path, env: dict[str, str] | None = None) -> RouteTable:
    env = dict(os.environ) if env is None else env
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError("routes.yaml 顶层应该是映射")
    missing: set[str] = set()
    raw = _substitute(raw, env, missing)
    if missing:
        raise ConfigError(f"缺环境变量：{', '.join(sorted(missing))}")

    gw = raw.get("gateway") or {}
    scalar = {
        "internal_ingress",
        "internal_suffix",
        "root_redirect",
        "cert_dir",
        "acme_root",
        "log_dir",
        "admin_listen",
        "https_port",
        "http_port",
    }
    extra = set(gw) - scalar - {"jwt", "jwks", "ban"}
    if extra:
        raise ConfigError(f"gateway 有不认识的字段：{sorted(extra)}")
    # 环境变量给了空值 = 用默认
    plain = {k: gw[k] for k in scalar if gw.get(k) not in (None, "")}
    if "internal_ingress" not in plain:
        raise ConfigError("gateway.internal_ingress 必填（内部 Ingress 的 IP:端口）")
    for port in ("https_port", "http_port"):
        if port in plain:
            plain[port] = int(plain[port])
    settings = Settings(
        **plain,
        jwt=_section(gw, "jwt", JwtSettings),
        jwks=_section(gw, "jwks", JwksSettings),
        ban=_section(gw, "ban", BanSettings),
    )

    sites = []
    for s in raw.get("sites") or []:
        if not s.get("host"):
            continue  # 可选站点：环境变量没给域名就不渲染
        try:
            sites.append(
                Site(host=str(s["host"]).lower(), kind=SiteKind(s["kind"]), name=s.get("name"))
            )
        except (KeyError, ValueError) as exc:
            raise ConfigError(f"站点写错了：{s}") from exc
    services = tuple(_service(s) for s in raw.get("services") or [])
    redirects = tuple(
        Redirect(source=str(r["from"]), target=str(r["to"])) for r in raw.get("redirects") or []
    )
    return RouteTable(settings=settings, sites=tuple(sites), services=services, redirects=redirects)
