"""UserCenterGateway：调用户中心的唯一客户端，错误只有一套口径。"""

from __future__ import annotations

from uuid import UUID

import httpx
import pytest
from atlas_knowledge.access.perms import Grant
from atlas_knowledge.access.usercenter import UserCenterGateway
from atlas_knowledge.errors import Conflict, Forbidden, Invalid, NotFound, Unavailable

TENANT = UUID(int=10)


def _gateway(handler: object) -> UserCenterGateway:
    return UserCenterGateway("http://uc", transport=httpx.MockTransport(handler))  # type: ignore[arg-type]


async def test_errors_map_the_same_way_for_every_call() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        code = req.url.params.get("data_id", "")
        if code == "down":
            return httpx.Response(502)
        if code == "app-rejected":
            return httpx.Response(403, json={"message": "调用方不是 atlas"})
        if code == "gone":
            return httpx.Response(404, json={"message": "数据不存在"})
        if code == "bad":
            return httpx.Response(422, json={"message": "参数不对"})
        return httpx.Response(400, json={"code": code, "message": code})

    gw = _gateway(handler)
    with pytest.raises(Unavailable):
        await gw.level("alice", "down")
    with pytest.raises(Unavailable):  # 用户中心拒绝的是本服务这个调用方：不当作「没权限」放过去
        await gw.level("alice", "app-rejected")
    with pytest.raises(NotFound):
        await gw.level("alice", "gone")
    with pytest.raises(Invalid):
        await gw.level("alice", "bad")
    with pytest.raises(Forbidden):
        await gw.level("alice", "OWNER_REQUIRED")
    with pytest.raises(Conflict):
        await gw.level("alice", "LAST_DATA_OWNER")
    assert await gw.acl("gone") == []
    await gw.aclose()

    def unreachable(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    gw = _gateway(unreachable)
    with pytest.raises(Unavailable):
        await gw.grant("ds", "alice", [Grant("READ", "user", "bob")])
    await gw.aclose()


async def test_snapshot_is_scoped_to_the_tenant_and_cached() -> None:
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if req.url.path == "/open/v1/users/ghost/authz":
            return httpx.Response(401)
        return httpx.Response(200, json={"can_access": True, "permissions": ["kb:create"]})

    gw = _gateway(handler)
    assert (await gw.snapshot("alice", TENANT))["permissions"] == ["kb:create"]
    assert calls[-1].headers["x-tenant-id"] == str(TENANT)
    assert calls[-1].headers["x-caller-app"] == "atlas"
    await gw.snapshot("alice", TENANT)
    assert len(calls) == 1, "成功结果缓存"
    with pytest.raises(NotFound):  # 不在这个租户、停用、不存在
        await gw.snapshot("ghost", TENANT)
    with pytest.raises(NotFound):
        await gw.snapshot("ghost", TENANT)
    assert len(calls) == 3, "失败不缓存"
    await gw.aclose()
