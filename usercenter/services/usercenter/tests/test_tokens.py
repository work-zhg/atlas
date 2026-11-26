"""访问 JWT + 刷新令牌。

设计见 c4-model/apigateway/code-auth.html、dynamic-web-jwt.html、dynamic-mobile-jwt.html。
"""

from __future__ import annotations

import base64
import json
from datetime import timedelta

import jwt
from atlas_usercenter.db.models import Session
from atlas_usercenter.db.session import get_sessionmaker
from atlas_usercenter.db.types import utcnow
from atlas_usercenter.security import hash_token, new_token
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy import select, update
from uc_testkit import STRONG, Client, OpsClient, admin_client, make_tenant, make_user, root_id


def _claims(token: str) -> dict:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


async def _verify_with_jwks(c: Client, token: str) -> dict:
    """像网关一样：只拿公开的 JWKS 验签。"""
    keys = (await c.get("/api/v1/auth/jwks")).json()["keys"]
    kid = jwt.get_unverified_header(token)["kid"]
    jwk = next(k for k in keys if k["kid"] == kid)
    raw = base64.urlsafe_b64decode(jwk["x"] + "=" * (-len(jwk["x"]) % 4))
    return jwt.decode(
        token,
        Ed25519PublicKey.from_public_bytes(raw),
        algorithms=["EdDSA"],
        audience="atlas-gateway",
        issuer="atlas-usercenter",
    )


async def test_web_login_sets_three_cookies_and_jwt_verifies_with_jwks() -> None:
    c = await admin_client()
    jar = {ck.name: ck for ck in c.http.cookies.jar}
    assert {"uc_access", "uc_refresh", "uc_csrf"} <= set(jar)
    assert "uc_session" not in jar
    assert jar["uc_refresh"].path == "/api/v1/auth"  # 平时的业务请求不带刷新令牌
    assert jar["uc_access"].path == "/" and jar["uc_access"].has_nonstandard_attr("HttpOnly")
    claims = await _verify_with_jwks(c, jar["uc_access"].value)
    me = (await c.get("/api/v1/auth/me")).json()
    assert claims["sub"] == me["user"]["id"] and claims["tid"] == me["tenant"]["id"]
    assert claims["mcp"] is False and claims["exp"] - claims["iat"] == 15 * 60 and claims["sid"]
    jwks = (await c.get("/api/v1/auth/jwks")).json()
    assert jwks["keys"][0]["kty"] == "OKP" and jwks["keys"][0]["alg"] == "EdDSA"
    await c.close()


async def test_refresh_rotates_with_grace() -> None:
    c = await admin_client()
    old = c.http.cookies.get("uc_refresh")
    r = await c.post("/api/v1/auth/refresh")
    assert r.status_code == 200, r.text
    # 访问令牌同一秒内签出来是一样的（Ed25519 签名是确定的），只比刷新令牌
    assert c.http.cookies.get("uc_refresh") != old and c.http.cookies.get("uc_access")
    # 宽限期内旧令牌还能换（两个标签页同时刷新、弱网重传）
    other = Client()
    other.http.cookies.set("uc_refresh", old, path="/api/v1/auth")
    other.http.cookies.set("uc_csrf", c.http.cookies.get("uc_csrf"))
    assert (await other.post("/api/v1/auth/refresh")).status_code == 200
    await c.close()
    await other.close()


async def test_stolen_refresh_token_kills_the_session() -> None:
    victim = await admin_client()
    stolen = victim.http.cookies.get("uc_refresh")
    # 攻击者先用偷来的令牌刷新
    thief = Client()
    thief.http.cookies.set("uc_refresh", stolen, path="/api/v1/auth")
    thief.http.cookies.set("uc_csrf", "x")
    assert (await thief.post("/api/v1/auth/refresh", headers={"X-UC-CSRF": "x"})).status_code == 200
    # 过了宽限期，本人拿旧令牌来刷新 = 发现令牌被盗用：整个会话作废，攻击者手里的新令牌也没用了
    async with get_sessionmaker()() as s:
        await s.execute(update(Session).values(rotated_at=utcnow() - timedelta(minutes=5)))
        await s.commit()
    r = await victim.post("/api/v1/auth/refresh")
    assert (r.status_code, r.json()["code"]) == (401, "REFRESH_REUSED")
    assert (await thief.post("/api/v1/auth/refresh", headers={"X-UC-CSRF": "x"})).status_code == 401
    assert (await thief.get("/api/v1/auth/me")).status_code == 401  # 会话没了，访问令牌也立刻不认
    await victim.close()
    await thief.close()


async def test_refresh_needs_csrf_and_a_valid_token() -> None:
    c = await admin_client()
    r = await c.http.post("/api/v1/auth/refresh")  # 不带 X-UC-CSRF
    assert r.status_code == 403 and r.json()["code"] == "CSRF_FAILED"
    anon = Client()
    anon.http.cookies.set("uc_csrf", "x")
    r = await anon.post("/api/v1/auth/refresh", headers={"X-UC-CSRF": "x"})
    assert r.status_code == 401 and r.json()["code"] == "REFRESH_INVALID"
    await c.close()
    await anon.close()


async def test_mobile_token_flow() -> None:
    await (await admin_client()).close()
    app = Client()
    body = {"account": "admin", "password": STRONG, "tenant": "default"}
    t = (await app.http.post("/api/v1/auth/token", json=body)).json()
    assert t["token_type"] == "Bearer" and t["expires_in"] == 900
    assert not app.http.cookies.get("uc_access")  # 手机不写 Cookie
    bearer = {"Authorization": f"Bearer {t['access_token']}"}
    assert (await app.http.get("/api/v1/auth/me", headers=bearer)).status_code == 200
    # Bearer 的写请求不校 CSRF
    r = await app.http.patch("/api/v1/me", json={"phone": "13800000000"}, headers=bearer)
    assert r.status_code == 200, r.text
    t2 = (
        await app.http.post(
            "/api/v1/auth/token/refresh", json={"refresh_token": t["refresh_token"]}
        )
    ).json()
    assert t2["refresh_token"] != t["refresh_token"]
    r = await app.http.post(
        "/api/v1/auth/token/revoke", json={"refresh_token": t2["refresh_token"]}
    )
    assert r.status_code == 200
    r = await app.http.post(
        "/api/v1/auth/token/refresh", json={"refresh_token": t2["refresh_token"]}
    )
    assert r.status_code == 401
    assert (await app.http.get("/api/v1/auth/me", headers=bearer)).status_code == 401
    bad = {"Authorization": "Basic eDp5"}
    assert (await app.http.get("/api/v1/auth/me", headers=bad)).json()["code"] == "TOKEN_INVALID"
    await app.close()


async def test_gateway_identity_headers() -> None:
    c = await admin_client()
    me = (await c.get("/api/v1/auth/me")).json()
    gw = Client()
    headers = {
        "X-Caller-App": "gateway",
        "X-Uid": me["user"]["id"],
        "X-Tenant-Id": me["tenant"]["id"],
    }
    r = await gw.http.get("/api/v1/me/apps", headers=headers)
    assert r.status_code == 200, r.text
    # 网关已校过 CSRF：经网关的写请求不再要双提交头
    r = await gw.http.patch("/api/v1/me", json={"phone": "13900000000"}, headers=headers)
    assert r.status_code == 200, r.text
    wrong = {**headers, "X-Tenant-Id": "00000000-0000-0000-0000-0000000000aa"}
    assert (await gw.http.get("/api/v1/me/apps", headers=wrong)).status_code == 401
    # 不是网关写的（没有 X-Caller-App: gateway）就不认这两个头
    plain = {"X-Uid": me["user"]["id"], "X-Tenant-Id": me["tenant"]["id"]}
    assert (await gw.http.get("/api/v1/me/apps", headers=plain)).status_code == 401
    await c.close()
    await gw.close()


async def test_password_change_keeps_this_login_only() -> None:
    admin = await admin_client()
    _, temp = await make_user(admin, "zhang.ming", await root_id(admin))
    phone = Client()
    await phone.login("zhang.ming", temp, tenant="default")  # 另一处登录（还在须改密状态）
    web = Client()
    r = await web.login("zhang.ming", temp, tenant="default")
    assert r.json()["must_change_password"] is True
    assert _claims(web.http.cookies.get("uc_access"))["mcp"] is True
    r = await web.post("/api/v1/auth/password", {"old_password": temp, "new_password": STRONG})
    assert r.status_code == 200, r.text
    # 浏览器顺手换了一个 mcp=false 的访问令牌；别处的登录全部作废
    assert _claims(web.http.cookies.get("uc_access"))["mcp"] is False
    assert (await web.get("/api/v1/me/apps")).status_code == 200
    assert (await web.post("/api/v1/auth/refresh")).status_code == 200
    assert (await phone.get("/api/v1/auth/me")).status_code == 401
    assert (await phone.post("/api/v1/auth/refresh")).status_code == 401
    for x in (admin, phone, web):
        await x.close()


async def test_old_gateway_verify_accepts_jwt() -> None:
    c = await admin_client()
    r = await c.get("/api/v1/auth/verify")
    assert r.status_code == 204 and r.headers["x-uid"]
    write = {"X-Original-Method": "POST"}
    assert (await c.http.get("/api/v1/auth/verify", headers=write)).status_code == 403
    r = await c.http.get(
        "/api/v1/auth/verify", headers={**write, "X-UC-CSRF": c.http.cookies.get("uc_csrf")}
    )
    assert r.status_code == 204
    bearer = Client()
    token = c.http.cookies.get("uc_access")
    r = await bearer.http.get(
        "/api/v1/auth/verify", headers={"Authorization": f"Bearer {token}", **write}
    )
    assert r.status_code == 204  # Bearer 不校 CSRF
    await c.close()
    await bearer.close()


async def test_legacy_session_cookie_still_works() -> None:
    c = await admin_client()
    me = (await c.get("/api/v1/auth/me")).json()
    raw = new_token()
    async with get_sessionmaker()() as s:
        s.add(
            Session(
                token_hash=hash_token(raw),
                user_uuid=me["user"]["id"],
                expires_at=utcnow() + timedelta(hours=1),
            )
        )
        await s.commit()
    old = Client()
    old.http.cookies.set("uc_session", raw)
    assert (await old.get("/api/v1/auth/me")).status_code == 200
    assert (await old.get("/api/v1/auth/verify")).status_code == 204
    # 登出把旧会话也作废
    await old.post("/api/v1/auth/logout")
    async with get_sessionmaker()() as s:
        assert await s.scalar(select(Session).where(Session.token_hash == hash_token(raw))) is None
    await c.close()
    await old.close()


async def test_logout_revokes_refresh_and_clears_cookies() -> None:
    c = await admin_client()
    access = c.http.cookies.get("uc_access")
    assert (await c.post("/api/v1/auth/logout")).status_code == 200
    assert not c.http.cookies.get("uc_access") and not c.http.cookies.get("uc_refresh")
    replay = Client()
    replay.http.cookies.set("uc_access", access)
    assert (await replay.get("/api/v1/auth/me")).status_code == 401
    await c.close()
    await replay.close()


async def test_lock_triggering_attempt_is_401_then_403() -> None:
    admin = await admin_client()
    _, temp = await make_user(admin, "li.si", await root_id(admin))
    c = Client()
    for _ in range(4):
        assert (await c.login("li.si", "wrong", tenant="default")).status_code == 401
    r = await c.login("li.si", "wrong", tenant="default")
    assert (r.status_code, r.json()["code"]) == (401, "ACCOUNT_LOCKED")
    r = await c.login("li.si", temp, tenant="default")
    assert (r.status_code, r.json()["code"]) == (
        403,
        "ACCOUNT_LOCKED",
    )  # 已锁定：不验密码、网关不计数
    await admin.close()
    await c.close()


async def test_refresh_refused_when_user_or_tenant_blocked() -> None:
    ops = OpsClient()
    acme_id, boss = await make_tenant(ops, "acme")
    assert (await boss.post("/api/v1/auth/refresh")).status_code == 200
    assert (await ops.post(f"/ops/v1/tenants/{acme_id}/block")).status_code == 200
    r = await boss.post("/api/v1/auth/refresh")
    assert (r.status_code, r.json()["code"]) == (401, "TENANT_DISABLED")
    assert (await ops.post(f"/ops/v1/tenants/{acme_id}/unblock")).status_code == 200
    assert (await boss.post("/api/v1/auth/refresh")).status_code == 200
    await boss.close()
    await ops.close()
