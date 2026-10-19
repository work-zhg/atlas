"""契约：知识库服务的用户中心适配器（atlas_knowledge.perms.UCDataPermissions）对着真的用户中心。

知识库自己的测试用内存替身；这里钉住替身背后的真实接口行为（参数、错误码、Owner 兜底、
「全部知识库」授权对未登记的库也生效）。
"""

from __future__ import annotations

import httpx
import pytest
from atlas_knowledge.errors import Conflict, Forbidden
from atlas_knowledge.perms import DATA_CODE, Grant, UCDataPermissions
from atlas_usercenter.api.app import create_app
from uc_testkit import admin_client, make_user, root_id


async def test_uc_data_permissions_adapter() -> None:
    c = await admin_client()
    root = await root_id(c)
    li, _ = await make_user(c, "li.gong", root)
    he, _ = await make_user(c, "he.chao", root)
    # 内网免令牌：知识库以 Atlas 应用的身份调用（X-Caller-App: atlas，内网身份传递 §3）
    r = await c.post("/api/v1/apps", {"name": "Atlas", "code": "atlas"})
    app_id = r.json()["app"]["id"]
    await c.put(f"/api/v1/apps/{app_id}/scope", {"all": True, "dept_ids": []})
    dt = (
        await c.post(f"/api/v1/apps/{app_id}/data-types", {"code": DATA_CODE, "name": "知识库"})
    ).json()

    perms = UCDataPermissions("http://uc", transport=httpx.ASGITransport(app=create_app()))
    try:
        assert await perms.root_dept() == root
        # 新建：建库人 OWNER + 全员（根部门含下级）只读
        await perms.create(
            "kb1", "制度库", [Grant("OWNER", "user", li), Grant("READ", "dept", root, True)]
        )
        assert await perms.level(li, "kb1") == "OWNER"
        assert await perms.level(he, "kb1") == "READ"
        ids, type_level = await perms.accessible(he, "READ")
        assert ids == {"kb1": "READ"} and type_level == "NONE"

        # 只有 Owner 能共享；Owner 把何超升为读写
        with pytest.raises(Forbidden):
            await perms.grant("kb1", he, [Grant("WRITE", "user", he)])
        await perms.grant("kb1", li, [Grant("WRITE", "user", he)])
        assert await perms.level(he, "kb1") == "WRITE"
        acl = await perms.acl("kb1")
        he_row = next(a for a in acl if a["subject"].get("id") == he)
        owner_row = next(a for a in acl if a["permission"] == "OWNER")
        await perms.update(he_row["id"], li, level="READ", include_sub=None)
        assert await perms.level(he, "kb1") == "READ"
        with pytest.raises(Conflict):  # 至少保留一个 Owner
            await perms.revoke(owner_row["id"], li)
        await perms.revoke(he_row["id"], li)

        # 「全部知识库 · Owner」：覆盖已登记的库，也覆盖还没登记的（存量服务账号的库）
        r = await c.post(
            f"/api/v1/data-types/{dt['id']}/acl",
            {"level": "OWNER", "subjects": [{"type": "user", "id": he}]},
        )
        assert r.status_code == 201, r.text
        assert await perms.level(he, "kb1") == "OWNER"
        assert await perms.level(he, "never-registered") == "OWNER"
        _, type_level = await perms.accessible(he, "READ")
        assert type_level == "OWNER"

        await perms.rename("kb1", "制度库（新）")
        await perms.cleanup("kb1")
        assert await perms.acl("kb1") == []
        assert await perms.level(li, "kb1") == "NONE"
    finally:
        await perms.aclose()
        await c.close()
