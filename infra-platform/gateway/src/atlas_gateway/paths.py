"""路由 → Nginx location 的对应规则（渲染和硬规则共用一份，免得两边对不上）。

  后端 /<名><子路径>：去掉 /<名> 转发；前端 /<名><子路径>：原样转发（basePath = /<名>）。
  子路径 /         → location /<名>/              （整个服务；前端另加 = /<名>，即 basePath 本身）
  以 / 结尾        → location /<名>/a/b/          （前缀）
  不以 / 结尾      → location = /<名>/a/b         （精确）
  含 *（占一整段） → location ~ ^/<名>/a/[^/]+/b$  （正则；以 / 结尾时不加 $）

★ 前缀 location 不加 ^~：正则 location 要能盖过同服务的前缀（如 /atlas/v1/ 里的 …/events）。
  所有正则都以 ^/<名>/ 开头，碰不到别的服务。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .model import Kind, Route, RouteTable, Service


def glob_regex(external: str) -> str:
    """/atlas/v1/threads/*/events → ^/atlas/v1/threads/[^/]+/events$

    段只含字母数字和 ._~-（硬规则保证），re.escape 之后不会有歧义。
    """
    prefix = external.endswith("/")
    segs = external.strip("/").split("/")
    parts = ["[^/]+" if s == "*" else re.escape(s) for s in segs]
    return "^/" + "/".join(parts) + ("/" if prefix else "$")


@dataclass(frozen=True)
class Location:
    route: Route
    #: location 指令的参数，如 "/atlas/v1/"、"= /device/connect"、"~ ^/atlas/…$"
    match: str
    #: 给人看的外部路径（日志、报错）
    external: str
    #: 后端去掉的前缀（/atlas、旧名 /atlas-api、旧路径则为 None 且用 upstream_uri 整段替换）
    strip: str | None
    #: 前缀 / 精确 location 的 proxy_pass URI；None = 原样（前端）或用 rewrite（正则）
    upstream_uri: str | None
    #: 正则 location 的 rewrite（去掉前缀）
    rewrite: str | None
    host: str

    @property
    def is_regex(self) -> bool:
        return self.match.startswith("~")

    @property
    def literal_len(self) -> int:
        return len(self.external.split("*", 1)[0])


def _location(route: Route, prefix: str, host: str, *, legacy_path: str | None = None) -> Location:
    external = legacy_path or (prefix + route.subpath)
    if route.is_glob:
        regex = glob_regex(external)
        rewrite = None
        if route.kind is Kind.API:
            rewrite = f"^{re.escape(prefix)}(/.*)$ $1 break"
        return Location(route, f"~ {regex}", external, prefix, None, rewrite, host)
    match = external if route.is_prefix else f"= {external}"
    upstream_uri = route.subpath if route.kind is Kind.API else None
    strip = None if legacy_path else (prefix if route.kind is Kind.API else None)
    return Location(route, match, external, strip, upstream_uri, None, host)


def service_locations(service: Service, internal_host: str) -> list[Location]:
    out: list[Location] = []
    prefixes = [f"/{service.name}"] + [f"/{n}" for n in service.legacy_names]
    for route in service.routes:
        for prefix in prefixes:
            out.append(_location(route, prefix, internal_host))
            if route.kind is Kind.UI and route.subpath == "/":
                # 前端的根（不带斜杠的 basePath）原样转过去：Next.js 把 /<名>-ui/ 308 到 /<名>-ui，
                # 只有前缀 location 的话 Nginx 又会 301 回 /<名>-ui/，来回跳
                out.append(Location(route, f"= {prefix}", prefix, None, None, None, internal_host))
        for lp in route.legacy_paths:
            out.append(_location(route, "", internal_host, legacy_path=lp))
    return out


def plan(table: RouteTable) -> list[Location]:
    """Atlas 站点的全部路由 location：正则按字面前缀从长到短（Nginx 正则按出现顺序取第一个）。"""
    locs: list[Location] = []
    for s in table.services:
        locs.extend(service_locations(s, table.settings.internal_host(s.name)))
    regex = sorted((loc for loc in locs if loc.is_regex), key=lambda loc: -loc.literal_len)
    plain = sorted((loc for loc in locs if not loc.is_regex), key=lambda loc: loc.external)
    return plain + regex


def redirect_matches(source: str) -> tuple[str, str]:
    """旧页面地址 /atlas 占用的两个 location：= /atlas 和 /atlas/。"""
    return f"= {source}", f"{source}/"
