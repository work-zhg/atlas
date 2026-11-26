"""公钥环的一生：没有公钥 → 503；用户中心发布 → 拉到并落盘；换钥 → 不认识的 kid 提前拉；
重启时用户中心不在 → 读缓存文件照常验签；reload 不丢公钥和封禁表。每个场景一个新容器。"""

from __future__ import annotations

import json
import time

import pytest
from gw_harness import Gateway, SigningKey


@pytest.fixture
def fresh(tmp_path, need_docker):
    g = Gateway(tmp_path)
    g.prepare()
    yield g
    g.stop()


def bearer(key: SigningKey, **claims) -> dict[str, str]:
    return {"Authorization": f"Bearer {key.token(**claims)}"}


def test_no_keys_then_published(fresh: Gateway) -> None:
    g = fresh
    g.start()  # 用户中心还没发布公钥，也没有缓存文件
    r = g.admin("GET", "/readyz")
    assert r.status == 503 and r.json()["ready"] is False
    k1 = SigningKey("k-1")
    r = g.get("/atlas/v1/threads", headers=bearer(k1))
    assert r.status == 503 and r.json()["code"] == "KEYS_UNAVAILABLE"
    # 页面、透传、self_auth 照常
    assert g.get("/atlas-ui/").status == 200
    assert g.post("/llm/v1/messages", headers={"Authorization": "Bearer sk"}).status == 200

    g.set_jwks(k1)
    g.wait_ready(timeout=10)  # 没拉成功前每 retry（测试里 1 秒）拉一次
    assert g.get("/atlas/v1/threads", headers=bearer(k1)).status == 200
    cached = json.loads((g.state / "cache" / "jwks.json").read_text())
    assert [k["kid"] for k in cached["keys"]] == ["k-1"]


def test_rotation_unknown_kid_triggers_refetch(fresh: Gateway) -> None:
    g = fresh
    k1, k2 = SigningKey("k-1"), SigningKey("k-2")
    g.set_jwks(k1)
    g.start()
    g.wait_ready()
    # 用户中心先发布新公钥，再用它签
    g.set_jwks(k1, k2)
    r = g.get("/atlas/v1/threads", headers=bearer(k2))
    if r.status == 401:  # 网关还没拉到：本次 401，后台提前拉一次
        assert r.json()["code"] == "TOKEN_INVALID"
        deadline = time.time() + 8
        while time.time() < deadline and r.status != 200:
            time.sleep(0.3)
            r = g.get("/atlas/v1/threads", headers=bearer(k2))
    assert r.status == 200, r.body
    assert g.get("/atlas/v1/threads", headers=bearer(k1)).status == 200
    # 撤掉旧钥：用它签的令牌随之失效
    g.set_jwks(k2)
    deadline = time.time() + 8
    while time.time() < deadline and g.get("/atlas/v1/threads", headers=bearer(k1)).status != 401:
        time.sleep(0.3)
    assert g.get("/atlas/v1/threads", headers=bearer(k1)).status == 401


def test_restart_without_usercenter_uses_cache(fresh: Gateway) -> None:
    g = fresh
    k1 = SigningKey("k-1")
    g.set_jwks(k1)
    g.start()
    g.wait_ready()
    assert (g.state / "cache" / "jwks.json").exists()
    g.stop()
    (g.state / "uc-jwks.json").unlink()  # 宿主机重启时用户中心还没起来
    g.name += "-2"
    g.start()
    r = g.admin("GET", "/readyz")
    assert r.status == 200 and r.json()["ready"] is True
    assert g.get("/atlas/v1/threads", headers=bearer(k1)).status == 200


def test_reload_keeps_keys_and_bans(fresh: Gateway) -> None:
    g = fresh
    k1 = SigningKey("k-1")
    g.set_jwks(k1)
    g.start()
    g.wait_ready()
    try:
        for _ in range(3):
            assert g.post("/uc/api/v1/auth/login", body=b'{"password":"bad"}').status == 401
        assert g.get("/atlas-ui/").status == 403
        out = g.exec(
            "openresty -t -q -c /etc/openresty/atlas/nginx.conf"
            " && openresty -s reload -c /etc/openresty/atlas/nginx.conf"
        )
        assert "emerg" not in out, out
        time.sleep(1)
        assert g.get("/atlas-ui/").status == 403  # 封禁表在共享内存里，reload 不丢
        assert g.admin("GET", "/readyz").status == 200
    finally:
        for b in g.admin("GET", "/bans").json()["bans"]:
            g.admin("DELETE", f"/bans/{b['ip']}")
    assert g.get("/atlas/v1/threads", headers=bearer(k1)).status == 200


def test_usercenter_issued_tokens_pass_the_gateway(fresh: Gateway) -> None:
    """契约：用户中心真正签出来的令牌和公钥集，网关认（alg、kid、iss、aud、sub、tid、mcp）。"""
    import uuid

    from atlas_usercenter.auth.tokens import KeyRing
    from atlas_usercenter.settings import UCSettings

    ring = KeyRing(UCSettings(jwt_signing_key="", jwt_published_keys=""))
    g = fresh
    g.set_jwks_text(json.dumps(ring.jwks()))
    g.start()
    g.wait_ready()
    user, tenant = uuid.uuid4(), uuid.uuid4()
    token = ring.issue(user, tenant, uuid.uuid4(), False)
    r = g.get("/atlas/v1/threads", headers={"Authorization": f"Bearer {token}"})
    assert r.status == 200, r.body
    h = r.json()["headers"]
    assert (h["x-uid"], h["x-tenant-id"]) == (str(user), str(tenant))
    must_change = ring.issue(user, tenant, None, True)
    r = g.get("/atlas/v1/threads", headers={"Authorization": f"Bearer {must_change}"})
    assert r.status == 403 and r.json()["code"] == "PASSWORD_CHANGE_REQUIRED"
