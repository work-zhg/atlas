"""ConfigRenderer：通过硬规则的路由表 → 宿主机上的 Nginx 配置。

设计见 c4-model/apigateway/code-routing.html。

输出目录（部署时就是 /etc/openresty/atlas）：
  nginx.conf          主配置（systemd 用 -c 指向它）
  sites.conf          每个站点一个 server；80 跳转与 ACME；443 拒绝未知域名；本机监控端口
  atlas-routes.conf   Atlas 站点的白名单 location
  errors.conf         网关自己的错误 → JSON（请求体太大、连不上、超时）
  vars.conf           网关自己的变量的默认值（每个 server include）
  proxy/*.conf        转发档位、公共转发头、身份头
  gateway.json        给 Lua 的设置：每条路由的属性、JWT、公钥集、封禁策略
  lua/atlas_gw/*.lua  Lua 模块（从包里拷过去，输出目录自成一体）

只在部署和 CI 里跑；出错只会让发布失败，不影响线上流量。
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .model import FORBIDDEN_SEGMENTS, AuthMode, RouteTable
from .paths import plan

#: 所有站点进 location 之前清掉的请求头：内网身份头 + 运营接口的签名头。
#: 逐个列出，不用 X-Ops-* 通配符（会误伤以后合法的同前缀头）
SCRUB_HEADERS = (
    "X-Uid",
    "X-Tenant-Id",
    "X-Auth-Uid",
    "X-Caller-App",
    "X-Ops-Timestamp",
    "X-Ops-Signature",
    "X-Ops-Operator",
    "X-Ops-Operator-Name",
    "X-Ops-Operation",
)

#: 经 error_page 转成 JSON 的错误：进了 location 之后 Nginx 自己产生的（请求体太大、连不上、超时）。
#: 5xx 一律「系统忙」，原因看网关日志。边界的 400 / 404 在 server 级直接回 JSON，不在这里
_ERRORS = (
    (413, "PAYLOAD_TOO_LARGE", "上传的内容太大"),
    (500, "GATEWAY_ERROR", "系统忙不过来了，请稍后再试"),
    (502, "UPSTREAM_UNAVAILABLE", "系统忙不过来了，请稍后再试"),
    (503, "UPSTREAM_UNAVAILABLE", "系统忙不过来了，请稍后再试"),
    (504, "UPSTREAM_TIMEOUT", "系统忙不过来了，请稍后再试"),
)


def error_body(code: str, message: str, request_id: str = "$request_id") -> str:
    """错误信封：{code, message, request_id}；另带 error.kind 和 detail，现有前端的解包都认得。"""
    return json.dumps(
        {
            "code": code,
            "message": message,
            "request_id": request_id,
            "error": {"kind": code.lower(), "message": message},
            "detail": message,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


@dataclass(frozen=True)
class _Error:
    status: int
    body: str


def _env() -> Environment:
    templates = resources.files("atlas_gateway") / "templates"
    return Environment(
        loader=FileSystemLoader(str(templates)),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        autoescape=False,
    )


def lua_settings(table: RouteTable) -> dict:
    """gateway.json：Lua 只读路由的属性，不比路径前缀（规则见 code-auth.html）。"""
    s = table.settings
    return {
        "routes": {
            r.id: {"pending_password_ok": r.pending_password_ok}
            for r in table.routes()
            if r.mode is AuthMode.AUTH
        },
        "jwt": {
            "issuer": s.jwt.issuer,
            "audience": s.jwt.audience,
            "clock_skew": s.jwt.clock_skew,
            "max_len": s.jwt.max_len,
            "access_cookie": s.jwt.access_cookie,
            "csrf_cookie": s.jwt.csrf_cookie,
            "csrf_header": s.jwt.csrf_header,
        },
        "jwks": {
            "addr": s.ingress_host,
            "port": s.ingress_port,
            "host": s.internal_host(s.jwks.service),
            "path": s.jwks.path,
            "cache": s.jwks.cache,
            "refresh": s.jwks.refresh,
            "retry": s.jwks.retry,
            "kid_refetch_gap": s.jwks.kid_refetch_gap,
            "timeout_ms": s.jwks.timeout_ms,
        },
        "ban": {
            "window": s.ban.window,
            "threshold": s.ban.threshold,
            "duration": s.ban.duration,
            "allow": list(s.ban.allow),
        },
    }


def render(
    table: RouteTable,
    out: Path,
    *,
    install_dir: str,
    tls_hosts: set[str] | None = None,
) -> list[Path]:
    """写出全部配置文件，返回写了哪些。tls_hosts 给了就只为这些站点生成 443（证书还没签好时）。"""
    env = _env()
    s = table.settings
    tls_sites = [site for site in table.sites if tls_hosts is None or site.host in tls_hosts]
    ctx = {
        "s": s,
        "install_dir": install_dir.rstrip("/"),
        "sites": table.sites,
        "tls_sites": tls_sites,
        "locations": plan(table),
        "redirects": table.redirects,
        "scrub_headers": SCRUB_HEADERS,
        "forbidden": FORBIDDEN_SEGMENTS,
        "errors": [_Error(st, error_body(code, msg)) for st, code, msg in _ERRORS],
        "invalid_path": error_body("INVALID_PATH", "请求路径不合法"),
        "not_found": error_body("NOT_FOUND", "没有这个地址"),
    }
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def write(name: str, text: str) -> None:
        path = out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append(path)

    for name in ("nginx.conf", "sites.conf", "atlas-routes.conf", "errors.conf"):
        write(name, env.get_template(f"{name}.j2").render(**ctx))
    write(
        "vars.conf",
        (resources.files("atlas_gateway") / "templates" / "vars.conf").read_text(encoding="utf-8"),
    )
    proxy_dir = resources.files("atlas_gateway") / "templates" / "proxy"
    for f in sorted(proxy_dir.iterdir(), key=lambda p: p.name):
        if f.name.endswith(".conf"):
            write(f"proxy/{f.name}", f.read_text(encoding="utf-8"))
    write("gateway.json", json.dumps(lua_settings(table), ensure_ascii=False, indent=2) + "\n")

    lua_src = Path(str(resources.files("atlas_gateway") / "lua"))
    lua_dst = out / "lua"
    if lua_dst.exists():
        shutil.rmtree(lua_dst)
    shutil.copytree(lua_src, lua_dst, ignore=shutil.ignore_patterns("*_test.lua"))
    written.extend(sorted(lua_dst.rglob("*.lua")))
    return written
