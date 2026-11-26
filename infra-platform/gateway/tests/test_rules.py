"""硬规则与路由规划：不起网关，只看 routes.yaml → location 的对应关系和违规检测。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from atlas_gateway.model import ConfigError, load
from atlas_gateway.paths import glob_regex, plan
from atlas_gateway.render import render
from atlas_gateway.rules import check

ROUTES = Path(__file__).resolve().parents[1] / "routes.yaml"
DEPLOY = Path(__file__).resolve().parents[3] / "deploy"
ENV = {"ATLAS_PUBLIC_HOST": "atlas.example.com", "GATEWAY_INTERNAL_INGRESS": "10.96.0.20:80"}


def table_from(tmp_path: Path, text: str, env: dict[str, str] | None = None):
    p = tmp_path / "routes.yaml"
    p.write_text(text, encoding="utf-8")
    return load(p, {**ENV, **(env or {})})


BASE = """
gateway:
  internal_ingress: "${GATEWAY_INTERNAL_INGRESS}"
sites:
  - {host: "${ATLAS_PUBLIC_HOST}", kind: atlas}
services:
  - {name: uc, kind: api}
  - {name: atlas-ui, kind: ui}
"""


def problems(tmp_path: Path, services: str, extra: str = "") -> list[str]:
    table = table_from(tmp_path, BASE + services + extra)
    return [str(p) for p in check(table)]


def test_real_routes_pass() -> None:
    table = load(ROUTES, ENV)
    assert check(table) == []


def test_missing_env_is_listed(tmp_path: Path) -> None:
    p = tmp_path / "routes.yaml"
    p.write_text(BASE, encoding="utf-8")
    with pytest.raises(ConfigError, match="ATLAS_PUBLIC_HOST, GATEWAY_INTERNAL_INGRESS"):
        load(p, {})


def test_optional_site_skipped_when_host_empty() -> None:
    table = load(ROUTES, ENV)
    assert [s.host for s in table.sites] == ["atlas.example.com"]
    table = load(ROUTES, {**ENV, "ATLAS_LANGFUSE_HOST": "langfuse.example.com"})
    assert [s.host for s in table.sites] == ["atlas.example.com", "langfuse.example.com"]


def test_env_numbers_become_ints() -> None:
    table = load(
        ROUTES,
        {**ENV, "GATEWAY_BAN_THRESHOLD": "3", "GATEWAY_BAN_ALLOW": "10.0.0.0/8, 192.168.1.7"},
    )
    assert table.settings.ban.threshold == 3
    assert table.settings.ban.allow == ("10.0.0.0/8", "192.168.1.7")


@pytest.mark.parametrize(
    ("services", "needle"),
    [
        ("  - {name: ops, kind: api}\n", "是禁区段"),
        ("  - {name: Bad_Name, kind: api}\n", "不是合法的域名标签"),
        ("  - {name: foo-ui, kind: api}\n", "种类和名字不一致"),
        ("  - {name: foo, kind: ui}\n", "种类和名字不一致"),
        ("  - name: foo\n    kind: api\n    routes: [{subpath: /v1/ops/}]\n", "含禁区段"),
        ("  - name: foo\n    kind: api\n    routes: [{subpath: /v1/../x}]\n", "不合法的段"),
        ("  - name: foo\n    kind: api\n    routes: [{subpath: /v1/a*/b}]\n", "不合法的段"),
        ("  - {name: foo, kind: api, proxy: WEBSOCKET}\n", "WEBSOCKET 只给显式声明的子路径"),
        ("  - {name: foo, kind: api, mode: public}\n", "public 只能用在预览站点"),
        (
            "  - name: foo\n    kind: api\n    routes: [{subpath: /login, login: true}]\n",
            "只能是 passthrough",
        ),
        (
            "  - name: foo\n    kind: api\n"
            "    routes: [{subpath: /x/, mode: self_auth, pending_password_ok: true}]\n",
            "只对 auth",
        ),
        ("  - {name: foo-ui, kind: ui, mode: auth}\n", "前端只能是 public"),
        (
            "  - name: foo\n    kind: api\n    routes: [{subpath: /a/}, {subpath: /a/}]\n",
            "登记了 2 次",
        ),
        ("  - {name: foo, kind: api, legacy_names: [uc]}\n", "用了 2 次"),
    ],
)
def test_rule_violations(tmp_path: Path, services: str, needle: str) -> None:
    found = problems(tmp_path, services)
    assert any(needle in p for p in found), found


def test_takeover_open_at_path_end_is_allowed(tmp_path: Path) -> None:
    # /takeovers/{id}/open 以禁区词结尾，不是禁区段
    found = problems(
        tmp_path,
        "  - name: foo\n    kind: api\n    routes: [{subpath: /v1/runs/*/takeovers/*/open}]\n",
    )
    assert found == []


def test_redirect_collides_with_whole_service(tmp_path: Path) -> None:
    found = problems(tmp_path, "", "redirects:\n  - {from: /uc, to: /atlas-ui}\n")
    assert any("location /uc/ 生成了 2 次" in p for p in found), found


def test_internal_ingress_must_be_ip(tmp_path: Path) -> None:
    table = table_from(tmp_path, BASE, {"GATEWAY_INTERNAL_INGRESS": "ingress.local:80"})
    assert any("IPv4:端口" in str(p) for p in check(table))


def test_missing_cert_is_marked(tmp_path: Path) -> None:
    table = load(ROUTES, ENV)
    found = check(table, cert_root=tmp_path)
    assert {p.missing_cert_host for p in found} == {"atlas.example.com"}


def test_glob_regex() -> None:
    assert glob_regex("/atlas/v1/threads/*/events") == "^/atlas/v1/threads/[^/]+/events$"
    assert glob_regex("/atlas/v1/x/*/") == "^/atlas/v1/x/[^/]+/"


def test_plan_strips_name_for_api_and_keeps_ui() -> None:
    locs = {loc.match: loc for loc in plan(load(ROUTES, ENV))}
    atlas = locs["/atlas/v1/"]
    assert (atlas.upstream_uri, atlas.host) == ("/v1/", "atlas.internal.techstz.com")
    legacy = locs["/atlas-api/v1/"]
    assert legacy.upstream_uri == "/v1/"
    ui = locs["/atlas-ui/"]
    assert ui.upstream_uri is None and ui.host == "atlas-ui.internal.techstz.com"
    root = locs["= /atlas-ui"]
    assert root.upstream_uri is None and root.route is ui.route
    exact = locs["= /device/connect"]
    assert exact.upstream_uri == "/connect"
    events = locs["~ ^/atlas/v1/threads/[^/]+/events$"]
    assert events.rewrite == "^/atlas(/.*)$ $1 break"
    preview = locs["/v1/previews/"]
    assert preview.upstream_uri == "/v1/previews/" and preview.route.preview


def test_plan_regex_before_nothing_and_sorted_by_specificity() -> None:
    locs = plan(load(ROUTES, ENV))
    regex = [loc for loc in locs if loc.is_regex]
    assert [loc.literal_len for loc in regex] == sorted(
        (loc.literal_len for loc in regex), reverse=True
    )


def test_render_writes_everything(tmp_path: Path) -> None:
    table = load(ROUTES, {**ENV, "GATEWAY_BAN_ALLOW": "10.0.0.0/8"})
    files = render(table, tmp_path / "out", install_dir="/etc/openresty/atlas")
    names = {p.relative_to(tmp_path / "out").as_posix() for p in files}
    assert {"nginx.conf", "sites.conf", "atlas-routes.conf", "errors.conf", "gateway.json"} <= names
    assert "lua/atlas_gw/jwt.lua" in names and "proxy/identity.conf" in names
    nginx = (tmp_path / "out" / "nginx.conf").read_text()
    assert (
        "more_clear_input_headers X-Uid X-Tenant-Id X-Auth-Uid X-Caller-App X-Ops-Timestamp"
        in nginx
    )
    assert "X-Ops-*" not in nginx
    routes = (tmp_path / "out" / "atlas-routes.conf").read_text()
    # 登录接口挂 log 阶段的计数；预览不记路径
    login = routes.split("location = /uc/api/v1/auth/login {", 1)[1].split("}\n", 3)
    assert 'atlas_gw.ban").record()' in "".join(login)
    assert (
        "gw_json_nopath"
        in routes.split("location /atlas/v1/previews/ {", 1)[1].split("\n}\n", 1)[0]
    )
    errors = (tmp_path / "out" / "errors.conf").read_text()
    assert '"request_id":"$request_id"' in errors


def test_lua_settings_only_auth_routes() -> None:
    import json

    from atlas_gateway.render import lua_settings

    s = lua_settings(load(ROUTES, ENV))
    assert s["routes"]["uc:/api/v1/me/"] == {"pending_password_ok": True}
    assert "uc:/api/v1/auth/login" not in s["routes"]
    assert s["jwks"] == {
        "addr": "10.96.0.20",
        "port": 80,
        "host": "uc.internal.techstz.com",
        "path": "/api/v1/auth/jwks",
        "cache": "/var/lib/atlas-gateway/jwks.json",
        "refresh": 300,
        "retry": 5,
        "kid_refetch_gap": 10,
        "timeout_ms": 3000,
    }
    json.dumps(s)


def _ingress_hosts() -> set[str]:
    hosts: set[str] = set()
    for f in [*DEPLOY.glob("local/*.yaml"), *DEPLOY.glob("server/*.yaml")]:
        for doc in yaml.safe_load_all(f.read_text(encoding="utf-8")):
            if doc and doc.get("kind") == "Ingress":
                hosts |= {r["host"] for r in doc["spec"]["rules"]}
    return hosts


def test_every_service_has_internal_ingress() -> None:
    """routes.yaml 的每个服务（和 Langfuse 透传站点）在部署清单里都有内部 Ingress。

    网关只把名字写进 Host，没有 Ingress 接就是 404。
    """
    table = load(ROUTES, {**ENV, "ATLAS_LANGFUSE_HOST": "langfuse.example.com"})
    s = table.settings
    want = {s.internal_host(svc.name) for svc in table.services} | {s.internal_host("langfuse")}
    assert sorted(want - _ingress_hosts()) == []
