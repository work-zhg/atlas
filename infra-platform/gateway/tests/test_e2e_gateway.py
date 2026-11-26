"""安全回归：对着 Docker 里的真网关（渲染好的配置 + 测试桩）跑，和实现无关，以后换实现也照跑。

覆盖设计里的几条线：边界（清头、畸形路径、禁区段、默认拒绝）、认证（JWT、CSRF、改密限制）、
路由（去前缀、Host、旧路径、跳转）、转发档位（SSE 不缓冲、WebSocket、请求体上限）、
透传站点、80 端口、
登录失败封禁。封禁会封掉测试机自己的 IP，放在最后，测完解封。
"""

from __future__ import annotations

import re
import ssl
import time
import uuid

import pytest
from gw_harness import ATLAS, LANGFUSE, Gateway, SigningKey

ORIGIN = f"https://{ATLAS}"
CSRF = "c" * 32


def cookie(token: str, csrf: str | None = CSRF) -> str:
    return f"uc_access={token}" + (f"; uc_csrf={csrf}" if csrf else "")


def assert_error(r, status: int, code: str) -> dict:
    assert r.status == status, (r.status, r.body[:300])
    body = r.json()
    assert body["code"] == code, body
    assert body["error"]["kind"] == code.lower()
    assert re.fullmatch(r"[0-9a-f]{32}", body["request_id"]), body
    assert body["message"] and body["detail"] == body["message"]
    return body


# ─────────────────────────────────────────────── 边界


def test_forged_identity_headers_scrubbed_on_public_route(gw: Gateway) -> None:
    forged = {
        "X-Uid": "evil",
        "X-Tenant-Id": "evil",
        "X-Auth-Uid": "evil",
        "X-Caller-App": "evil",
        "X-Ops-Signature": "evil",
        "X-Ops-Operator": "evil",
        "X-Forwarded-For": "6.6.6.6",
        "X-Other-Ops": "keep",
    }
    r = gw.get("/atlas-ui/threads", headers=forged)
    assert r.status == 200, r.body
    echo = r.json()
    h = echo["headers"]
    for name in (
        "x-uid",
        "x-tenant-id",
        "x-auth-uid",
        "x-caller-app",
        "x-ops-signature",
        "x-ops-operator",
    ):
        assert name not in h, (name, h)
    assert h["x-other-ops"] == "keep"
    assert h["x-forwarded-for"] != "6.6.6.6"
    assert echo["host"] == "atlas-ui.internal.techstz.com"
    assert echo["uri"] == "/atlas-ui/threads"


@pytest.mark.parametrize(
    "path",
    [
        "/atlas/v1/..%252finternal/keys",
        "/atlas/v1/a%5cb",
        "/atlas/v1/a\\b",
        "/atlas/v1/%2e%2e/internal/x",
        "/atlas-ui/a%0Ab",
    ],
)
def test_malformed_path_is_400(gw: Gateway, path: str) -> None:
    assert_error(gw.get(path), 400, "INVALID_PATH")


def test_nul_is_rejected_by_nginx_itself(gw: Gateway) -> None:
    # 解析请求行时 Nginx 自己就拒了（没有 URI，回的是 Nginx 默认的 400 页），到不了任何 location
    assert gw.get("/atlas/v1/a%00b").status == 400


def test_percent_in_query_is_fine(gw: Gateway) -> None:
    r = gw.get("/atlas-ui/search?q=100%25&x=%5c")
    assert r.status == 200
    assert r.json()["uri"] == "/atlas-ui/search?q=100%25&x=%5c"


@pytest.mark.parametrize(
    "path",
    [
        "/atlas/ops/v1/tenants",
        "/atlas-api/ops/v1/tenants",
        "/uc/api/open/v1/users",
        "/atlas/v1/x/internal/y",
        "/atlas/v1/x/%69nternal/y",
        "/atlas/v1/a/../internal/y",
        "/knowledge/OPS/v1/jobs",
        "/opscenter/api/systems/mcp/ops/configs",
    ],
)
def test_forbidden_segments_are_404(gw: Gateway, path: str) -> None:
    assert_error(gw.get(path), 404, "NOT_FOUND")


def test_forbidden_word_at_path_end_is_not_forbidden(gw: Gateway) -> None:
    # Atlas 的接管接口 /v1/runs/{id}/takeovers/{id}/open：以禁区词结尾，照常走认证
    assert_error(gw.post("/atlas/v1/runs/r1/takeovers/a1/open"), 401, "UNAUTHENTICATED")


@pytest.mark.parametrize(
    "path", ["/minio/", "/llm/healthz", "/device/internal/x", "/nope", "/uc-ui-x/"]
)
def test_unregistered_is_404(gw: Gateway, path: str) -> None:
    assert_error(gw.get(path), 404, "NOT_FOUND")


def test_root_redirect_and_bare_ui_root(gw: Gateway) -> None:
    r = gw.get("/")
    assert (r.status, r.header("location")) == (302, "/atlas-ui")
    # 不带斜杠的前端根原样转给前端（Next.js 把 /atlas-ui/ 308 到这里；网关再 301 回去就成了死循环）
    r = gw.get("/atlas-ui?x=1")
    assert r.status == 200 and r.json()["uri"] == "/atlas-ui?x=1"
    assert r.json()["host"] == "atlas-ui.internal.techstz.com"


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("/atlas", "/atlas-ui"),
        ("/atlas/threads/1?x=1", "/atlas-ui/threads/1?x=1"),
        ("/uc/login?next=/atlas", "/uc-ui/login?next=/atlas"),
        ("/teamflow/processes", "/teamflow-ui/processes"),
    ],
)
def test_old_page_paths_redirect(gw: Gateway, old: str, new: str) -> None:
    r = gw.get(old)
    assert (r.status, r.header("location")) == (301, new)


# ─────────────────────────────────────────────── 认证


def test_no_token_is_401(gw: Gateway) -> None:
    assert_error(gw.get("/atlas/v1/threads"), 401, "UNAUTHENTICATED")


def test_cookie_get_forwards_identity(gw: Gateway) -> None:
    sub, tid = str(uuid.uuid4()), str(uuid.uuid4())
    token = gw.key.token(sub=sub, tid=tid)
    r = gw.get("/atlas/v1/threads?limit=5", headers={"Cookie": cookie(token), "X-Uid": "evil"})
    assert r.status == 200, r.body
    echo = r.json()
    h = echo["headers"]
    assert echo["uri"] == "/v1/threads?limit=5"
    assert echo["host"] == "atlas.internal.techstz.com"
    assert (h["x-uid"], h["x-tenant-id"], h["x-caller-app"]) == (sub, tid, "gateway")
    assert "cookie" not in h and "authorization" not in h and "x-auth-uid" not in h
    assert h["x-forwarded-host"] == ATLAS and h["x-forwarded-proto"] == "https"
    assert re.fullmatch(r"[0-9a-f]{32}", h["x-request-id"])


def test_bearer_write_needs_no_csrf(gw: Gateway) -> None:
    r = gw.post("/atlas/v1/threads/t1/runs", headers={"Authorization": f"Bearer {gw.key.token()}"})
    assert r.status == 200, r.body
    assert "authorization" not in r.json()["headers"]


def test_cookie_write_csrf(gw: Gateway) -> None:
    token = gw.key.token()
    url = "/atlas/v1/threads/t1/runs"
    ok = {"Cookie": cookie(token), "X-UC-CSRF": CSRF, "Origin": ORIGIN}
    assert gw.post(url, headers=ok).status == 200
    assert gw.post(url, headers={**ok, "Origin": "https://evil.test"}).status == 403
    assert_error(
        gw.post(url, headers={k: v for k, v in ok.items() if k != "X-UC-CSRF"}), 403, "CSRF_FAILED"
    )
    assert_error(gw.post(url, headers={**ok, "X-UC-CSRF": "d" * 32}), 403, "CSRF_FAILED")
    assert_error(gw.post(url, headers={**ok, "Cookie": cookie(token, None)}), 403, "CSRF_FAILED")
    no_origin = {k: v for k, v in ok.items() if k != "Origin"}
    assert_error(gw.post(url, headers=no_origin), 403, "CSRF_FAILED")
    assert gw.post(url, headers={**no_origin, "Sec-Fetch-Site": "same-origin"}).status == 200
    assert gw.post(url, headers={**no_origin, "Sec-Fetch-Site": "cross-site"}).status == 403


@pytest.mark.parametrize(
    ("claims", "code"),
    [
        ({"exp": int(time.time()) - 100}, "TOKEN_EXPIRED"),
        ({"iss": "someone-else"}, "TOKEN_INVALID"),
        ({"aud": "other"}, "TOKEN_INVALID"),
        ({"sub": "not-a-uuid"}, "TOKEN_INVALID"),
        ({"tid": "x\r\nX-Uid: evil"}, "TOKEN_INVALID"),
        ({"nbf": int(time.time()) + 600}, "TOKEN_INVALID"),
        ({"exp": "soon"}, "TOKEN_INVALID"),
    ],
)
def test_bad_claims(gw: Gateway, claims: dict, code: str) -> None:
    r = gw.get("/atlas/v1/threads", headers={"Authorization": f"Bearer {gw.key.token(**claims)}"})
    assert_error(r, 401, code)


def test_clock_skew_and_audience_list(gw: Gateway) -> None:
    for claims in ({"exp": int(time.time()) - 10}, {"aud": ["x", "atlas-gateway"]}):
        r = gw.get(
            "/atlas/v1/threads", headers={"Authorization": f"Bearer {gw.key.token(**claims)}"}
        )
        assert r.status == 200, (claims, r.body)


def test_algorithm_confusion_and_forged_signature(gw: Gateway) -> None:
    good = gw.key.token()
    h, p, _ = good.split(".")
    import base64
    import hashlib
    import hmac
    import json

    def b64(d: bytes) -> str:
        return base64.urlsafe_b64encode(d).rstrip(b"=").decode()

    none_hdr = b64(json.dumps({"alg": "none", "kid": "k-1"}).encode())
    hs_hdr = b64(json.dumps({"alg": "HS256", "kid": "k-1"}).encode())
    hs_sig = b64(
        hmac.new(gw.key.jwk()["x"].encode(), f"{hs_hdr}.{p}".encode(), hashlib.sha256).digest()
    )
    other = SigningKey("k-1").token()  # 同一个 kid，别人的私钥
    tampered = f"{h}.{b64(json.dumps({'sub': str(uuid.uuid4())}).encode())}.{good.split('.')[2]}"
    for token in (
        f"{none_hdr}.{p}.",
        f"{hs_hdr}.{p}.{hs_sig}",
        other,
        tampered,
        "abc",
        "a.b.c",
        good + "x",
    ):
        r = gw.get("/atlas/v1/threads", headers={"Authorization": f"Bearer {token}"})
        assert_error(r, 401, "TOKEN_INVALID")


def test_non_bearer_authorization_does_not_fall_back_to_cookie(gw: Gateway) -> None:
    r = gw.get(
        "/atlas/v1/threads",
        headers={"Authorization": "Basic YTpi", "Cookie": cookie(gw.key.token())},
    )
    assert_error(r, 401, "TOKEN_INVALID")


def test_must_change_password(gw: Gateway) -> None:
    auth = {"Authorization": f"Bearer {gw.key.token(mcp=True)}"}
    assert_error(gw.get("/atlas/v1/threads", headers=auth), 403, "PASSWORD_CHANGE_REQUIRED")
    r = gw.get("/uc/api/v1/me/apps", headers=auth)
    assert r.status == 200 and r.json()["uri"] == "/api/v1/me/apps"


# ─────────────────────────────────────────────── 路由与转发


def test_passthrough_keeps_cookie_and_drops_identity(gw: Gateway) -> None:
    r = gw.post("/uc/api/v1/auth/refresh", headers={"Cookie": "uc_refresh=rt", "X-Uid": "evil"})
    assert r.status == 200
    echo = r.json()
    assert echo["uri"] == "/api/v1/auth/refresh" and echo["host"] == "uc.internal.techstz.com"
    assert echo["headers"]["cookie"] == "uc_refresh=rt"
    assert "x-uid" not in echo["headers"] and "x-caller-app" not in echo["headers"]


def test_login_passthrough_returns_upstream_cookie(gw: Gateway) -> None:
    r = gw.post("/uc/api/v1/auth/login", body=b'{"password":"good"}')
    assert r.status == 200 and "uc_access=tok" in (r.header("set-cookie") or "")


def test_self_auth_keeps_key(gw: Gateway) -> None:
    r = gw.post(
        "/llm/v1/messages", headers={"Authorization": "Bearer sk-atlas-xyz", "X-Uid": "evil"}
    )
    echo = r.json()
    assert echo["uri"] == "/v1/messages" and echo["host"] == "llm.internal.techstz.com"
    assert echo["headers"]["authorization"] == "Bearer sk-atlas-xyz"
    assert "x-uid" not in echo["headers"]
    assert (
        gw.post("/mcp/mcp/serpapi", headers={"Authorization": "Bearer mk-1"}).json()["uri"]
        == "/mcp/serpapi"
    )


def test_legacy_names_and_paths(gw: Gateway) -> None:
    auth = {"Authorization": f"Bearer {gw.key.token()}"}
    for path, uri, host in (
        ("/atlas-api/v1/threads", "/v1/threads", "atlas"),
        ("/atlas-knowledge-api/v1/datasets", "/v1/datasets", "knowledge"),
        ("/mcp-gateway-api/api/v1/services", "/api/v1/services", "mcp"),
        ("/skill-api/api/v1/skills", "/api/v1/skills", "skill"),
    ):
        echo = gw.get(path, headers=auth).json()
        assert (echo["uri"], echo["host"]) == (uri, f"{host}.internal.techstz.com"), path
    for path in ("/v1/previews/tok/index.html", "/atlas/v1/previews/tok/index.html"):
        echo = gw.get(path).json()  # 预览站点：public，不要令牌
        assert echo["uri"] == "/v1/previews/tok/index.html"
    assert gw.get("/device-api/pair?code=1").json()["uri"] == "/pair?code=1"
    assert gw.get("/device/u/up-1/v1/messages").json()["uri"] == "/u/up-1/v1/messages"


def test_sse_is_not_buffered(gw: Gateway) -> None:
    got = gw.stream(
        "/atlas/v1/threads/t1/events?after_seq=3", {"Cookie": cookie(gw.key.token())}, b"data: 2"
    )
    first = next(t for t, chunk in got if b"data: 1" in chunk)
    last = next(t for t, chunk in got if b"data: 2" in chunk)
    assert first < 1.0, got
    assert last - first > 1.0, got


def test_websocket_only_on_declared_route(gw: Gateway) -> None:
    sub = str(uuid.uuid4())
    headers = {"Cookie": cookie(gw.key.token(sub=sub)), "Origin": ORIGIN}
    resp, text = gw.websocket("/atlas/v1/threads/t1/browser/view", headers, "hi")
    assert resp.status == 101, resp.body
    assert text == f"echo:hi:{sub}"
    # 握手靠 Cookie：跨站页面发起的要挡住（浏览器会带 Origin）
    resp, _ = gw.websocket(
        "/atlas/v1/threads/t1/browser/view", {**headers, "Origin": "https://evil.test"}, "hi"
    )
    assert resp.status == 403
    # 没声明 WEBSOCKET 的路由不会把 Upgrade 转给上游
    resp, _ = gw.websocket("/atlas/v1/threads/t1/other", headers, "hi")
    assert resp.status == 200 and "upgrade" not in resp.json()["headers"]


def test_body_limits_by_profile(gw: Gateway) -> None:
    auth = {"Authorization": f"Bearer {gw.key.token()}"}
    assert_error(
        gw.raw("POST", "/atlas/v1/threads", headers=auth, content_length=21 * 1024 * 1024),
        413,
        "PAYLOAD_TOO_LARGE",
    )
    size = 30 * 1024 * 1024
    r = gw.raw("POST", "/knowledge/v1/datasets/d1/documents", headers=auth, body=b"x" * size)
    assert r.status == 200 and r.json()["body_len"] == size


def test_passthrough_site(gw: Gateway) -> None:
    r = gw.get("/api/public/traces", host=LANGFUSE, headers={"X-Uid": "evil", "Cookie": "lf=1"})
    echo = r.json()
    assert echo["host"] == "langfuse.internal.techstz.com" and echo["uri"] == "/api/public/traces"
    assert "x-uid" not in echo["headers"] and echo["headers"]["cookie"] == "lf=1"
    assert r.header("strict-transport-security")


def test_unknown_sni_is_rejected(gw: Gateway) -> None:
    with pytest.raises(ssl.SSLError):
        gw.connect(gw.ports[443], "evil.test").close()


def test_port_80(gw: Gateway) -> None:
    r = gw.get("/.well-known/acme-challenge/tok-123", port=80, tls=False)
    assert (r.status, r.body) == (200, b"acme-proof")
    r = gw.get("/atlas-ui/x?y=1", port=80, tls=False)
    assert (r.status, r.header("location")) == (301, f"https://{ATLAS}/atlas-ui/x?y=1")
    assert gw.get("/", host="evil.test", port=80, tls=False).status == 0  # 444：直接关连接


def test_readyz(gw: Gateway) -> None:
    r = gw.admin("GET", "/readyz")
    assert r.status == 200 and r.json()["ready"] is True


# ─────────────────────────────────────────────── 登录失败封禁（最后跑：会封掉测试机自己的 IP）


def test_login_failures_ban_the_ip(gw: Gateway) -> None:
    try:
        # 别的 401（没登录、令牌过期）不数
        for _ in range(5):
            assert gw.get("/atlas/v1/threads").status == 401
        assert gw.post("/uc/api/v1/auth/login", body=b'{"password":"bad"}').status == 401
        assert gw.post("/uc/api/v1/auth/token", body=b'{"password":"bad"}').status == 401
        # 登录成功不清零
        assert gw.post("/uc/api/v1/auth/login", body=b'{"password":"good"}').status == 200
        assert gw.get("/atlas-ui/").status == 200
        # 第 3 次失败（测试里阈值是 3）→ 封
        assert gw.post("/uc/api/v1/auth/login", body=b'{"password":"bad"}').status == 401
        body = assert_error(gw.get("/atlas-ui/"), 403, "IP_BLOCKED")
        assert "2 分钟" in body["message"]
        assert gw.get("/", host=LANGFUSE).status == 403  # 所有站点都封
        bans = gw.admin("GET", "/bans").json()["bans"]
        assert len(bans) == 1 and bans[0]["minutes_left"] == 2
        ip = bans[0]["ip"]
        assert gw.admin("DELETE", f"/bans/{ip}").json() == {"ip": ip, "was_banned": True}
        assert gw.get("/atlas-ui/").status == 200
    finally:
        for b in gw.admin("GET", "/bans").json()["bans"]:
            gw.admin("DELETE", f"/bans/{b['ip']}")
