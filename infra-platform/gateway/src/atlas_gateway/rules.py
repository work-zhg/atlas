"""硬规则（SafetyRules）：违反任何一条就不生成配置（c4-model/apigateway/code-routing.html）。

替代人工评审 location：禁区段、种类与名字、认证模式与档位的搭配都在这里一次查完，
CI 里 `python -m atlas_gateway --check` 跑同一套。
"""

from __future__ import annotations

import ipaddress
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .model import FORBIDDEN_SEGMENTS, AuthMode, Kind, ProxyProfile, RouteTable, SiteKind
from .paths import plan, redirect_matches

_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_SEGMENT = re.compile(r"^[A-Za-z0-9._~-]+$")
_FORBIDDEN = re.compile(r"/(" + "|".join(FORBIDDEN_SEGMENTS) + r")/", re.IGNORECASE)
_IPV4_PORT = re.compile(r"^(\d{1,3}\.){3}\d{1,3}:\d{1,5}$")


@dataclass(frozen=True)
class Problem:
    where: str
    message: str
    #: 缺证书的站点（只有这一类可以用 --allow-missing-certs 放过：先起 80 给 certbot 验证）
    missing_cert_host: str | None = None

    def __str__(self) -> str:
        return f"{self.where}：{self.message}"


def _path_problems(where: str, path: str, *, allow_glob: bool) -> list[Problem]:
    out: list[Problem] = []
    if not path.startswith("/"):
        return [Problem(where, f"路径 {path!r} 要以 / 开头")]
    if path == "/":
        return out
    segs = path.strip("/").split("/")
    for seg in segs:
        if seg == "*":
            if not allow_glob:
                out.append(Problem(where, f"路径 {path!r} 不能用 *"))
            continue
        if seg in ("", ".", "..") or not _SEGMENT.match(seg):
            out.append(
                Problem(
                    where,
                    f"路径 {path!r} 里有不合法的段 {seg!r}"
                    "（只能是字母、数字和 ._~-，* 要占一整段）",
                )
            )
    # 和网关的禁区段同一个口径：只挡「/段/」。精确路径以禁区词结尾（…/takeovers/{id}/open）不算
    if _FORBIDDEN.search(path):
        out.append(Problem(where, f"路径 {path!r} 含禁区段 /internal/、/ops/、/open/"))
    return out


def check(table: RouteTable, *, cert_root: Path | None = None) -> list[Problem]:
    """全部硬规则。cert_root 给了就检查每个站点的证书文件在不在（部署时；CI 不查）。"""
    p: list[Problem] = []
    s = table.settings

    # ── 站点
    if not any(site.kind is SiteKind.ATLAS for site in table.sites):
        p.append(Problem("sites", "至少要有一个 Atlas 站点"))
    for host, n in Counter(site.host for site in table.sites).items():
        if n > 1:
            p.append(Problem("sites", f"域名 {host} 登记了 {n} 次"))
    for site in table.sites:
        if site.kind is SiteKind.PASSTHROUGH and not (site.name and _LABEL.match(site.name)):
            p.append(Problem(f"站点 {site.host}", "透传站点要有合法的 name（拼内部域名用）"))
        if site.kind is SiteKind.ATLAS and site.name:
            p.append(Problem(f"站点 {site.host}", "Atlas 站点不写 name"))
        if cert_root is not None:
            for f in ("fullchain.pem", "privkey.pem"):
                if not (cert_root / site.host / f).is_file():
                    p.append(
                        Problem(
                            f"站点 {site.host}",
                            f"没有证书 {cert_root / site.host / f}",
                            missing_cert_host=site.host,
                        )
                    )

    # ── 服务与路由
    names = Counter(sv.name for sv in table.services)
    all_names = Counter(
        [sv.name for sv in table.services] + [n for sv in table.services for n in sv.legacy_names]
    )
    for name, n in all_names.items():
        if n > 1:
            p.append(Problem("services", f"名字 {name} 用了 {n} 次（含旧名）"))
    for sv in table.services:
        where = f"服务 {sv.name}"
        for name in (sv.name, *sv.legacy_names):
            if not _LABEL.match(name):
                p.append(Problem(where, f"名字 {name!r} 不是合法的域名标签"))
            if name in FORBIDDEN_SEGMENTS:
                p.append(Problem(where, f"名字 {name!r} 是禁区段，整个服务都会被 404"))
        if (sv.kind is Kind.UI) != sv.name.endswith("-ui"):
            p.append(Problem(where, "种类和名字不一致：ui 的名字以 -ui 结尾，api 的不以 -ui 结尾"))
        if sv.kind is Kind.UI and sv.legacy_names:
            p.append(Problem(where, "前端不用旧名（basePath 变了），旧页面地址用 redirects"))
        for sub, n in Counter(r.subpath for r in sv.routes).items():
            if n > 1:
                p.append(Problem(where, f"子路径 {sub} 登记了 {n} 次"))
        for r in sv.routes:
            rw = f"路由 {r.id}"
            p.extend(_path_problems(rw, r.subpath, allow_glob=True))
            if r.proxy is ProxyProfile.WEBSOCKET and r.subpath == "/":
                p.append(Problem(rw, "WEBSOCKET 只给显式声明的子路径，不能整个服务都升级"))
            if r.kind is Kind.UI and r.mode is not AuthMode.PUBLIC:
                p.append(Problem(rw, "前端只能是 public"))
            if r.kind is Kind.API and r.mode is AuthMode.PUBLIC and not r.preview:
                p.append(Problem(rw, "后端的 public 只能用在预览站点（preview: true）"))
            if r.preview and r.mode is not AuthMode.PUBLIC:
                p.append(Problem(rw, "preview 路由要是 public"))
            if r.login and r.mode is not AuthMode.PASSTHROUGH:
                p.append(Problem(rw, "登录接口（login）只能是 passthrough"))
            if r.pending_password_ok and r.mode is not AuthMode.AUTH:
                p.append(Problem(rw, "pending_password_ok 只对 auth 路由有意义"))
            for lp in r.legacy_paths:
                p.extend(_path_problems(rw, lp, allow_glob=False))
                if r.is_glob:
                    p.append(Problem(rw, "带 * 的路由不支持 legacy_paths"))
                elif lp.endswith("/") != r.is_prefix:
                    p.append(Problem(rw, f"旧路径 {lp} 和子路径 {r.subpath} 要同为前缀或同为精确"))

    # ── 渲染出来的 location 不能重复（含旧名、旧路径、旧页面跳转、根路径）
    seen: Counter[str] = Counter(loc.match for loc in plan(table))
    for rd in table.redirects:
        where = f"跳转 {rd.source}"
        name = rd.source.lstrip("/")
        if not (rd.source.startswith("/") and _LABEL.match(name)):
            p.append(Problem(where, "来源只能是 /<名字>"))
        target = table.service(rd.target.strip("/"))
        if target is None or target.kind is not Kind.UI:
            p.append(Problem(where, f"目标 {rd.target} 要是一个登记过的前端"))
        seen.update(redirect_matches(rd.source))
    for match, n in seen.items():
        if n > 1:
            p.append(Problem("locations", f"location {match} 生成了 {n} 次"))
    root = table.service(s.root_redirect.strip("/"))
    if root is None or root.kind is not Kind.UI:
        p.append(Problem("gateway.root_redirect", f"{s.root_redirect} 要是一个登记过的前端"))

    # ── 网关设置
    if not _IPV4_PORT.match(s.internal_ingress):
        p.append(
            Problem(
                "gateway.internal_ingress",
                "要写成 IPv4:端口（内部 Ingress 的固定 ClusterIP）：公钥环直连它，不解析域名",
            )
        )
    if names.get(s.jwks.service) is None or table.service(s.jwks.service).kind is not Kind.API:  # type: ignore[union-attr]
        p.append(Problem("gateway.jwks.service", f"{s.jwks.service} 不是登记过的后端"))
    for cidr in s.ban.allow:
        try:
            ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            p.append(Problem("gateway.ban.allow", f"{cidr!r} 不是合法的网段"))
    if s.ban.threshold < 1 or s.ban.window < 1 or s.ban.duration < 1:
        p.append(Problem("gateway.ban", "window、threshold、duration 都要是正数"))
    return p
