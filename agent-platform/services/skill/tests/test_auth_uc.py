"""身份来自请求头（网关写入 X-Uid），操作码按 X-Auth-Uid 取用户中心授权快照
（内网身份传递 §3、§6），按操作码鉴权。

★ 用户中心用 httpx.MockTransport 模拟，不碰真实服务。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from uuid import uuid4

import httpx
import pytest
from atlas_skill import auth
from atlas_skill.api.app import create_app
from atlas_skill.settings import get_settings

SNAPSHOTS = {
    "u-reviewer": {"can_access": True, "permissions": ["skill:review"]},
    "u-member": {"can_access": True, "permissions": ["chat:use"]},
    "u-outsider": {"can_access": False, "permissions": []},
}


@pytest.fixture
def uc_mode(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, str | None]]]:
    calls: list[dict[str, str | None]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        # 内网免令牌：以技能应用的身份调用，透传本次请求的身份
        assert "authorization" not in req.headers
        assert req.headers["x-caller-app"] == "skill"
        uid = req.url.path.split("/")[4]
        calls.append({"auth_uid": uid, "x-tenant-id": req.headers.get("x-tenant-id")})
        body = SNAPSHOTS.get(uid)
        return httpx.Response(200, json=body) if body else httpx.Response(404, json={})

    monkeypatch.setattr(auth, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(auth, "_cache", auth._Cache())
    get_settings.cache_clear()
    yield calls
    get_settings.cache_clear()


@pytest.fixture
async def client(storage, uc_mode) -> AsyncIterator[httpx.AsyncClient]:  # type: ignore[no-untyped-def]
    app = create_app(storage=storage)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://skill"
    ) as c:
        yield c


async def test_uc_identity_headers_and_operations(
    client: httpx.AsyncClient, uc_mode: list[dict[str, str | None]]
) -> None:
    # 没有 X-Uid（没经过网关）：401；旧的 X-User-Id 不再认
    assert (await client.get("/api/v1/skills")).status_code == 401
    r = await client.get("/api/v1/skills", headers={"X-User-Id": "u-reviewer"})
    assert r.status_code == 401
    # 用户中心里没有这个人：401；不在技能应用的可访问范围：403
    assert (await client.get("/api/v1/skills", headers={"X-Uid": "ghost"})).status_code == 401
    r = await client.get("/api/v1/skills", headers={"X-Uid": "u-outsider"})
    assert r.status_code == 403

    # 在可访问范围内就能看目录，不要操作码；审计、审查要操作码
    reviewer = {"X-Uid": "u-reviewer", "X-Tenant-Id": "t-1"}
    assert (await client.get("/api/v1/skills", headers=reviewer)).status_code == 200
    assert uc_mode[-1] == {"auth_uid": "u-reviewer", "x-tenant-id": "t-1"}
    member = {"X-Uid": "u-member"}
    assert (await client.get("/api/v1/skills", headers=member)).status_code == 200
    assert (await client.get("/api/v1/audit", headers=member)).status_code == 403
    r = await client.post(f"/api/v1/skill-uploads/{uuid4()}/approve", json={}, headers=member)
    assert r.status_code == 403

    # /me 给前端决定显示哪些入口
    me = (await client.get("/api/v1/me", headers=member)).json()
    assert me == {"uid": "u-member", "can_upload": False, "can_review": False, "can_audit": False}
    me = (await client.get("/api/v1/me", headers=reviewer)).json()
    assert (me["can_upload"], me["can_review"], me["can_audit"]) == (True, True, False)

    # 操作码按 X-Auth-Uid 判：成员在用、按审核人的权限
    on_behalf = {"X-Uid": "u-member", "X-Auth-Uid": "u-reviewer"}
    me = (await client.get("/api/v1/me", headers=on_behalf)).json()
    assert (me["uid"], me["can_review"]) == ("u-member", True)

    # 写请求不再要 CSRF 头（网关 + 用户中心统一校验）：过了鉴权，因上传不存在而 404
    r = await client.post(
        f"/api/v1/skill-uploads/{uuid4()}/approve", json={"note": "ok"}, headers=reviewer
    )
    assert r.status_code == 404, r.text

    # 快照有缓存：同一个人只问一次用户中心
    assert sum(1 for c in uc_mode if c["auth_uid"] == "u-reviewer") == 1
