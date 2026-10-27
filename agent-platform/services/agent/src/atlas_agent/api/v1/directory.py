"""通讯录（权限设计 §12.1）：共享智能体 / 知识库时选人、选部门。转发用户中心开放接口。

★ 只返回选人需要的字段（id、姓名、账号、部门）；范围 = Atlas 在用户中心的可访问范围、本租户内。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from ...auth.dependency import uc_client

router = APIRouter(prefix="/directory", tags=["directory"])


@router.get("/users")
async def users(
    request: Request,
    q: str | None = None,
    limit: int = Query(20, ge=1, le=50),
    digital: bool = False,
) -> list[dict[str, Any]]:
    """digital=true：连数字员工一起（把知识库共享给某个 Agent 时选它，数字员工设计 §6）。"""
    uc = uc_client(request)
    return [
        {
            "id": u["id"],
            "name": u["name"],
            "account": u["account"],
            "dept_id": u.get("dept_id"),
            "user_type": u.get("user_type") or "human",
        }
        for u in await uc.search_users(q, size=limit, digital=digital)
    ]


@router.get("/depts")
async def depts(request: Request) -> list[dict[str, Any]]:
    uc = uc_client(request)
    return [
        {"id": d["id"], "parent_id": d.get("parent_id"), "name": d["name"]}
        for d in await uc.depts_tree()
    ]
