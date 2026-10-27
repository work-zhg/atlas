"""用户中心（mock）：授权的唯一事实源 —— 谁对哪条数据有什么级别。数据放内存，代替用户中心的库。

只做本例用到的三件事：
    POST /data-objects   应用登记自己的数据对象（MCP 网关登记服务时调），登记人成为所有者
    POST /grants         所有者给人 / 数字员工授"使用"（MCP 管理端经网关调）
    GET  /accessible     某个主体能用（≥ 使用）哪些数据对象（MCP 网关判权时调）

★ 数据编码只能由所属应用登记和查询："MCP 服务"这个编码属于 MCP 网关，Atlas 不直接来问。
★ 不知道 MCP 怎么调用，只回答"谁能用哪条数据"。
★ 本例没有部门、编码级授权（"全部 MCP 服务"）：只有直接授给主体的。
"""

from __future__ import annotations

from hops import hop
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

LEVELS = {"use": 1, "owner": 2}
LEVEL_TEXT = {"use": "使用", "owner": "所有者"}


def app() -> Starlette:
    #: (数据编码, 数据标识) → 所属应用
    objects: dict[tuple[str, str], str] = {}
    #: (数据编码, 数据标识) → {主体: 级别}
    grants: dict[tuple[str, str], dict[str, str]] = {}

    async def register(request: Request) -> JSONResponse:
        b = await request.json()
        key = (b["code"], b["object_id"])
        if key in objects:
            return JSONResponse({"error": "已登记"}, status_code=409)
        objects[key] = b["app"]
        grants[key] = {b["owner"]: "owner"}
        hop(
            "用户中心",
            f"登记数据对象 {b['code']}/{b['object_id']}（应用 {b['app']}），所有者 {b['owner']}",
        )
        return JSONResponse({"ok": True})

    async def grant(request: Request) -> JSONResponse:
        b = await request.json()
        key = (b["code"], b["object_id"])
        if grants.get(key, {}).get(b["operator"]) != "owner":
            hop("用户中心", f"拒绝授权：{b['operator']} 不是 {b['object_id']} 的所有者")
            return JSONResponse({"error": "只有所有者能授权"}, status_code=403)
        grants[key][b["subject"]] = b["level"]
        hop(
            "用户中心",
            f"{b['operator']} 把 {b['object_id']} 的"
            f"「{LEVEL_TEXT[b['level']]}」授给 {b['subject']}",
        )
        return JSONResponse({"ok": True})

    async def accessible(request: Request) -> JSONResponse:
        app_, code, subject = (request.query_params[k] for k in ("app", "code", "subject"))
        found = sorted(
            oid
            for (c, oid), levels in grants.items()
            if c == code
            and objects[(c, oid)] == app_
            and LEVELS.get(levels.get(subject, ""), 0) >= LEVELS["use"]
        )
        hop("用户中心", f"{subject} 能用的 {code}：{found}")
        return JSONResponse({"object_ids": found})

    return Starlette(
        routes=[
            Route("/data-objects", register, methods=["POST"]),
            Route("/grants", grant, methods=["POST"]),
            Route("/accessible", accessible, methods=["GET"]),
        ]
    )
