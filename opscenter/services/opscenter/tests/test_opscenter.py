"""运营中心（c4-model/opscenter/）：运营人员与模块权限、租户的开通 / 停用 / 恢复 / 关闭 / 补建、
系统代理。


权限     运营人员是用户中心里系统租户的人（经网关的身份头进来）；按模块授「全部」级别：只读看、
        读写改、Owner 做不可恢复的；查审计要 ops:audit；普通租户的人进不来；系统租户不能停用、关闭
开通     第一步在接口里同步跑，临时密码只在响应里；之后按登记顺序调各系统的创建（带上开通时给它
        填的参数，如份额）；失败退避重试，可以立即重做
停用     只挡住用户中心的入口，不通知业务系统（它们不知道租户状态）；恢复只放开入口
关闭     只能关已停用的、企业代码要输对；各系统分批删到没有剩，最后删用户中心里的租户
补建     新登记的系统：对现有每个租户调一次它的创建
到期     不另外发起停用：用户中心把到期算作挡住
代理     只放行白名单（应用目录、份额…）；建租户、创建、删除、挡住只能经租户操作
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from atlas_opscenter.db.models import AuditEntry, OperationStep
from atlas_opscenter.db.session import get_sessionmaker
from atlas_opscenter.gateway import sent_as_is, stamp_of
from conftest import ALL_MODULES, Console, World, admin_console, operator, system_tenant, uc_user
from sqlalchemy import select, update


async def _open(c: Console, code: str = "xinghe", **extra: Any) -> dict[str, Any]:
    body = {
        "code": code,
        "name": "星河科技",
        "admin": {"account": "wangwu", "name": "王五", "email": f"wangwu@{code}.example"},
        "max_users": 50,
        **extra,
    }
    r = await c.post("/api/tenants", body)
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


async def _uc_tenant(c: Console, tenant_id: str) -> dict[str, Any]:
    r = await c.get(f"/api/tenants/{tenant_id}")
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


async def _settle(world: World, rounds: int = 10) -> None:
    for _ in range(rounds):
        await world.sweeper.tick()


async def _due_now() -> None:
    """把退避等待拨到现在（测试不真等）。"""
    async with get_sessionmaker()() as s:
        await s.execute(
            update(OperationStep)
            .where(OperationStep.status == "FAILED")
            .values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await s.commit()


# ───────────────────────────── 运营人员与模块权限


async def test_without_gateway_identity_is_401_and_other_tenants_are_403(world: World) -> None:
    assert (await Console(world.ops).get("/api/me")).json()["code"] == "NOT_LOGGED_IN"
    await world.ops.state.catalog_sync.run()
    # 普通租户（default）的人：用户中心的快照是不可访问（运营中心只能开给系统租户）
    from atlas_usercenter.db.models import Tenant
    from atlas_usercenter.db.session import get_sessionmaker as uc_sessions
    from atlas_usercenter.db.tenancy import platform_scope

    async with uc_sessions()() as s:
        with platform_scope(s):
            dflt = await s.scalar(select(Tenant).where(Tenant.code == "default"))
    outsider = Console(world.ops, await uc_user(dflt.uuid, "zhang.san"), dflt.uuid)
    r = await outsider.get("/api/me")
    assert (r.status_code, r.json()["code"]) == (403, "NOT_OPERATOR")


async def test_module_levels_decide_what_one_can_do(world: World) -> None:
    c = await operator(world.ops, {"OpsAlpha": "WRITE", "OpsBeta": "READ"}, account="xiao.zhou")
    me = (await c.get("/api/me")).json()
    levels = {m["code"]: m["level"] for m in me["modules"]}
    assert levels == {
        "OpsTenant": "NONE",
        "OpsUserCenter": "NONE",
        "OpsAlpha": "WRITE",
        "OpsBeta": "READ",
    }
    assert me["can_audit"] is False
    # 甲：能看能改；乙：只能看；租户模块没授 → 连列表都看不到；审计要 ops:audit
    assert (await c.get("/api/systems/alpha/proxy/capacity")).status_code == 200
    some = "00000000-0000-0000-0000-0000000000a1"
    r = await c.send("PUT", f"/api/systems/alpha/proxy/quotas/{some}", {"storage_bytes": 1})
    assert r.status_code == 200, r.text
    r = await c.send("PUT", f"/api/systems/beta/proxy/quotas/{some}", {"storage_bytes": 1})
    assert r.json()["code"] in ("MODULE_FORBIDDEN", "NOT_ALLOWED")
    r = await c.get("/api/tenants")
    assert (r.status_code, r.json()["code"]) == (403, "MODULE_FORBIDDEN")
    assert (await c.get("/api/audit")).json()["code"] == "PERMISSION_DENIED"
    r = await c.get("/api/systems/usercenter/proxy/apps")
    assert r.json()["code"] == "MODULE_FORBIDDEN"
    # 审计记在模块下
    async with get_sessionmaker()() as s:
        rows = list(await s.scalars(select(AuditEntry).where(AuditEntry.action == "system.put")))
    assert [(r.module, r.operator_name) for r in rows] == [("OpsAlpha", "xiao.zhou")]


async def test_irreversible_operations_need_owner(world: World) -> None:
    boss = await admin_console(world.ops)
    tenant = (await _open(boss))["tenant"]["id"]
    await _settle(world)
    c = await operator(world.ops, {"OpsTenant": "WRITE", "OpsUserCenter": "WRITE"}, account="li.si")
    assert (await c.post(f"/api/tenants/{tenant}/suspend", {})).status_code == 202
    await _settle(world)
    r = await c.post(f"/api/tenants/{tenant}/close", {"confirm_code": "xinghe"})
    assert (r.status_code, r.json()["code"]) == (403, "MODULE_FORBIDDEN")
    r = await c.post(f"/api/tenants/{tenant}/admin-password")
    assert r.json()["code"] == "MODULE_FORBIDDEN"
    # 用户中心模块：建应用读写就够，轮换凭据要 Owner
    app = (
        await c.post("/api/systems/usercenter/proxy/apps", {"name": "客户管理", "code": "crm"})
    ).json()
    r = await c.post(f"/api/systems/usercenter/proxy/apps/{app['app']['id']}/rotate-secret")
    assert r.json()["code"] == "MODULE_FORBIDDEN"
    r = await boss.post(f"/api/systems/usercenter/proxy/apps/{app['app']['id']}/rotate-secret")
    assert r.status_code == 200, r.text
    # 给租户开通应用归租户模块（不归用户中心模块）
    only_uc = await operator(world.ops, {"OpsUserCenter": "OWNER"}, account="wang.wu")
    r = await only_uc.send(
        "PUT",
        f"/api/systems/usercenter/proxy/tenants/{tenant}/apps/{app['app']['id']}",
        {"enabled": True},
    )
    assert r.json()["code"] == "MODULE_FORBIDDEN"


async def test_system_tenant_cannot_be_suspended_or_closed(world: World) -> None:
    boss = await admin_console(world.ops)
    sys_tenant, _ = await system_tenant()
    r = await boss.post(f"/api/tenants/{sys_tenant}/suspend", {})
    assert (r.status_code, r.json()["code"]) == (409, "SYSTEM_TENANT")
    r = await boss.post(f"/api/tenants/{sys_tenant}/close", {"confirm_code": "platform"})
    assert (r.status_code, r.json()["code"]) == (409, "SYSTEM_TENANT")
    rows = {t["code"]: t for t in (await boss.get("/api/tenants")).json()}
    assert rows["platform"]["is_system"] is True


async def test_catalog_sync_registers_every_module_and_opens_once(world: World) -> None:
    sync = world.ops.state.catalog_sync
    await sync.run()
    await sync.run()  # 幂等
    boss = await admin_console(world.ops)
    apps = (await boss.get("/api/systems/usercenter/proxy/apps")).json()
    app = next(a for a in apps if a["code"] == "opscenter")
    assert app["system_only"] is True
    types = (await boss.get(f"/api/systems/usercenter/proxy/apps/{app['id']}/data-types")).json()
    assert sorted(d["code"] for d in types) == sorted(ALL_MODULES)


async def test_open_hands_over_the_temp_password_once_then_creates_in_each_system(
    world: World,
) -> None:
    c = await admin_console(world.ops)
    quota = {"running_runs": 20, "sandboxes": 30, "storage_bytes": 1024}
    out = await _open(c, quotas={"alpha": quota}, params={"beta": {"flag": True}})
    assert out["temp_password"] and out["admin"]["account"] == "wangwu"
    tid = out["tenant"]["id"]
    assert [(s["system"], s["action"], s["status"]) for s in out["operation"]["steps"]] == [
        ("usercenter", "CREATE_TENANT", "DONE"),
        ("alpha", "CREATE", "PENDING"),
        ("beta", "CREATE", "PENDING"),
    ]

    await _settle(world)
    # 各系统的创建带上开通时给它填的参数：份额并进 alpha 的参数里
    assert world.alpha.created[tid] == quota
    assert world.beta.created[tid] == {"flag": True}
    detail = await _uc_tenant(c, tid)
    assert detail["tenant"]["code"] == "xinghe" and detail["tenant"]["status"] == "active"
    assert detail["operations"][0]["status"] == "DONE"
    # 临时密码不进审计；「发起」的审计在操作结束后补记了结果
    async with get_sessionmaker()() as s:
        rows = list(await s.scalars(select(AuditEntry).where(AuditEntry.action == "tenant.open")))
    assert len(rows) == 1 and rows[0].result == "OK"
    assert out["temp_password"] not in str(rows[0].detail)

    # 首个管理员没登录过：可以补发（旧的作废）；之后的列表里能看到它
    r = await c.post(f"/api/tenants/{tid}/admin-password")
    assert r.status_code == 200 and r.json()["temp_password"] != out["temp_password"]
    listed = {t["code"]: t for t in (await c.get("/api/tenants")).json()}
    assert listed["xinghe"]["operation"] is None
    await c.close()


async def test_open_params_only_for_systems_that_take_tenants(world: World) -> None:
    c = await admin_console(world.ops)
    body = {
        "code": "xinghe",
        "name": "星河科技",
        "admin": {"account": "wangwu", "name": "王五", "email": "w@x.example"},
    }
    r = await c.post("/api/tenants", {**body, "params": {"usercenter": {}}})
    assert r.json()["code"] == "VALIDATION_FAILED"
    r = await c.post("/api/tenants", {**body, "quotas": {"beta": {"storage_bytes": 1}}})
    assert r.json()["code"] == "VALIDATION_FAILED", "beta 没有份额"
    await c.close()


async def test_a_rejected_open_leaves_nothing_behind(world: World) -> None:
    c = await admin_console(world.ops)
    await _open(c)
    r = await c.post(
        "/api/tenants",
        {
            "code": "xinghe",
            "name": "重名",
            "admin": {"account": "a.b", "name": "a", "email": "a@b.c"},
        },
    )
    assert r.json()["code"] == "TENANT_REJECTED" and "企业代码已存在" in r.json()["message"]
    async with get_sessionmaker()() as s:
        from atlas_opscenter.db.models import TenantOperation

        kinds = [o.kind for o in await s.scalars(select(TenantOperation))]
    assert kinds == ["OPEN"], "被拒绝的那次开通没有留下操作"
    await c.close()


async def test_a_failing_system_is_retried_with_the_same_key(world: World) -> None:
    c = await admin_console(world.ops)
    world.beta.fail["create"] = 1
    out = await _open(c)
    tid, op_id = out["tenant"]["id"], out["operation"]["id"]

    await _settle(world, 3)
    op = (await _uc_tenant(c, tid))["operations"][0]
    assert op["status"] == "RUNNING" and op["stuck"] is True
    failed = next(s for s in op["steps"] if s["status"] == "FAILED")
    assert failed["system"] == "beta" and "暂时不可用" in failed["last_error"]
    # 有进行中的操作时不能再发起
    r = await c.post(f"/api/tenants/{tid}/suspend", {})
    assert r.json()["code"] == "OPERATION_RUNNING"

    # 立即重做：不等退避
    assert (await c.post(f"/api/operations/{op_id}/retry")).status_code == 200
    await _settle(world)
    assert (await _uc_tenant(c, tid))["operations"][0]["status"] == "DONE"
    assert [a for a, _ in world.beta.calls] == ["create", "create"]
    # 重做用的是同一个幂等键
    assert world.beta.idempotency[0] == world.beta.idempotency[1]
    await c.close()


async def test_a_rejected_step_backs_off_longer_and_is_marked(world: World) -> None:
    c = await admin_console(world.ops)
    world.alpha.reject.add("create")
    tid = (await _open(c))["tenant"]["id"]
    await _settle(world)
    step = next(
        s for s in (await _uc_tenant(c, tid))["operations"][0]["steps"] if s["system"] == "alpha"
    )
    assert step["status"] == "FAILED" and step["rejected"] is True
    wait = datetime.fromisoformat(step["next_attempt_at"]) - datetime.now(UTC)
    assert wait > timedelta(minutes=4), "被拒绝的退避更长"
    world.alpha.reject.clear()
    await _due_now()
    await _settle(world)
    assert (await _uc_tenant(c, tid))["operations"][0]["status"] == "DONE"
    await c.close()


# ───────────────────────────── 停用、恢复、关闭


async def test_suspend_and_resume_only_touch_the_user_center(world: World) -> None:
    c = await admin_console(world.ops)
    tid = (await _open(c))["tenant"]["id"]
    await _settle(world)
    calls = (list(world.alpha.calls), list(world.beta.calls))

    r = await c.post(f"/api/tenants/{tid}/suspend", {"reason": "欠费"})
    assert r.status_code == 202, r.text
    assert [(s["system"], s["action"]) for s in r.json()["steps"]] == [("usercenter", "BLOCK")]
    await _settle(world)
    assert (await _uc_tenant(c, tid))["tenant"]["status"] == "suspended"

    r = await c.post(f"/api/tenants/{tid}/resume")
    assert [(s["system"], s["action"]) for s in r.json()["steps"]] == [("usercenter", "UNBLOCK")]
    await _settle(world)
    assert (await _uc_tenant(c, tid))["tenant"]["status"] == "active"
    # 业务系统不知道租户状态：停用、恢复都没调它们
    assert (world.alpha.calls, world.beta.calls) == calls
    await c.close()


async def test_close_only_a_suspended_tenant_and_delete_until_nothing_is_left(
    world: World,
) -> None:
    c = await admin_console(world.ops)
    tid = (await _open(c))["tenant"]["id"]
    await _settle(world)
    r = await c.post(f"/api/tenants/{tid}/close", {"confirm_code": "xinghe"})
    assert r.json()["code"] == "TENANT_NOT_SUSPENDED"
    await c.post(f"/api/tenants/{tid}/suspend", {})
    await _settle(world)
    r = await c.post(f"/api/tenants/{tid}/close", {"confirm_code": "nope"})
    assert r.json()["code"] == "CONFIRM_MISMATCH"

    world.alpha.purge_batches = 3
    r = await c.post(f"/api/tenants/{tid}/close", {"confirm_code": "XINGHE"})
    assert r.status_code == 202, r.text
    assert [(s["system"], s["action"]) for s in r.json()["steps"]] == [
        ("alpha", "DELETE"),
        ("beta", "DELETE"),
        ("usercenter", "DELETE_TENANT"),
    ]
    await _settle(world)
    assert [a for a, _ in world.alpha.calls].count("delete") == 3
    # 用户中心最后删：租户没了，运营库里的操作记录还在（带企业代码快照）
    assert (await c.get(f"/api/tenants/{tid}")).status_code == 404
    async with get_sessionmaker()() as s:
        steps = list(await s.scalars(select(OperationStep).where(OperationStep.action == "DELETE")))
    alpha_step = next(st for st in steps if st.system == "alpha")
    assert alpha_step.status == "DONE" and alpha_step.result == {"deleted": {"rows": 30}}
    await c.close()


async def test_steps_planned_before_the_upgrade_still_finish(world: World) -> None:
    """升级前排好、还没做完的步骤（停用时挂起各系统、关闭时 purge）照新契约做完。"""
    from uuid import uuid4

    from atlas_opscenter.db.models import TenantOperation
    from atlas_opscenter.db.types import utcnow

    c = await admin_console(world.ops)
    tid = (await _open(c))["tenant"]["id"]
    await _settle(world)
    op_uuid = uuid4()
    async with get_sessionmaker()() as s:
        s.add(
            TenantOperation(
                operation_uuid=op_uuid,
                tenant_uuid=tid,
                tenant_code="xinghe",
                kind="SUSPEND",
                status="RUNNING",
            )
        )
        for seq, (system, action) in enumerate(
            [("alpha", "SUSPEND"), ("alpha", "PURGE"), ("beta", "INSTALL_SKILLS")], start=1
        ):
            s.add(
                OperationStep(
                    step_uuid=uuid4(),
                    operation_uuid=op_uuid,
                    seq=seq,
                    system=system,
                    action=action,
                    status="PENDING",
                    attempts=0,
                    next_attempt_at=utcnow(),
                    rejected=False,
                )
            )
        await s.commit()
    before = len(world.alpha.calls)
    await _settle(world)
    op = next(o for o in (await _uc_tenant(c, tid))["operations"] if o["id"] == str(op_uuid))
    assert op["status"] == "DONE"
    assert [a for a, _ in world.alpha.calls[before:]] == ["delete"], "挂起不再调业务系统"
    assert world.beta.created[tid] == {}, "装默认技能并进了创建"
    await c.close()


# ───────────────────────────── 补建


async def test_backfill_creates_a_new_system_for_every_existing_tenant(world: World) -> None:
    c = await admin_console(world.ops)
    tid = (await _open(c))["tenant"]["id"]
    await _settle(world)
    world.beta.calls.clear()
    world.beta.created.clear()

    r = await c.post("/api/systems/beta/backfill")
    assert r.status_code == 202, r.text
    tenants = (await c.get("/api/tenants")).json()
    assert r.json() == {"started": len(tenants), "skipped": []}
    await _settle(world)
    assert set(world.beta.created) == {t["id"] for t in tenants} and tid in world.beta.created
    # 只接租户的系统能补建；用户中心不行
    r = await c.post("/api/systems/usercenter/backfill")
    assert r.json()["code"] == "VALIDATION_FAILED"
    await c.close()


# ───────────────────────────── 系统代理、到期


async def test_proxy_passes_the_allowlist_only_and_audits_writes(world: World) -> None:
    c = await admin_console(world.ops)
    systems = {s["code"]: s for s in (await c.get("/api/systems")).json()}
    assert systems["alpha"]["capabilities"] == ["capacity", "lifecycle"]
    r = await c.get("/api/systems/usercenter/proxy/apps")
    assert r.status_code == 200 and any(a["is_builtin"] for a in r.json())
    r = await c.post("/api/systems/usercenter/proxy/apps", {"name": "客户管理", "code": "crm"})
    assert r.status_code == 201 and r.json()["app_secret"]
    assert (await c.get("/api/systems/alpha/proxy/capacity")).json() == {
        "usage": {"storage_bytes": 1}
    }
    # 建租户、创建、删除、挡住不能从代理绕过租户操作
    for method, path in (
        ("POST", "/api/systems/usercenter/proxy/tenants"),
        ("DELETE", "/api/systems/usercenter/proxy/tenants/00000000-0000-0000-0000-000000000001"),
        (
            "POST",
            "/api/systems/usercenter/proxy/tenants/00000000-0000-0000-0000-000000000001/block",
        ),
        ("PUT", "/api/systems/alpha/proxy/tenants/00000000-0000-0000-0000-000000000001"),
        ("DELETE", "/api/systems/alpha/proxy/tenants/00000000-0000-0000-0000-000000000001"),
    ):
        r = await c.send(method, path, {})
        assert r.status_code == 403 and r.json()["code"] == "NOT_ALLOWED", (method, path)
    # ★ 点段绕白名单：/apps/../tenants 会被 httpx 规整成 /tenants，带着运营签名直达白名单外的
    #   接口。客户端发 %2e%2e（httpx 不规整编码过的点），进路由时解码成 ..
    # ★ ? # 同理：..%23 解码成 ..#，httpx 把 # 之后当片段，发出去的是 /ops/v1；
    #   %252f 解码一次是 %2f，匹配时是一段、发出去被当成 /
    for method, path in (
        ("POST", "/api/systems/usercenter/proxy/apps/%2e%2e/tenants"),
        ("GET", "/api/systems/alpha/proxy/capacity/%2e%2e/tenant-states"),
        ("POST", "/api/systems/usercenter/proxy/apps/..%23"),
        ("GET", "/api/systems/usercenter/proxy/apps/..%3fx"),
        ("GET", "/api/systems/usercenter/proxy/apps/..%252f..%252ftenants"),
    ):
        r = await c.send(method, path, {})
        assert r.status_code == 422 and r.json()["code"] == "BAD_PATH", (method, path)
    async with get_sessionmaker()() as s:
        rows = list(await s.scalars(select(AuditEntry).where(AuditEntry.action == "system.post")))
    assert [r.target for r in rows] == ["usercenter /apps"]
    assert "app_secret" not in str(rows[0].detail)
    await c.close()


async def test_paths_with_non_ascii_characters_pass_the_signature(world: World) -> None:
    """★ 签名两边的路径编码要一致：业务系统验的是解码后的路径。原先签编码后的 raw_path，
    路径里有中文、空格时一律 401。"""
    reply = await world.ops.state.ops_client.request(
        "alpha", "GET", "/echo/技能 市场", params={"q": "名 字"}, stamp=stamp_of(None, "测试")
    )
    assert reply.status == 200, reply.body
    assert reply.json() == {"rest": "技能 市场", "query": {"q": "名 字"}}


def test_only_paths_httpx_sends_unchanged_are_forwarded() -> None:
    for path in ("/apps", "/configs/技能 市场", "/apps/100%"):
        assert sent_as_is(path), path
    for path in (
        "/apps/..",
        "/apps/../tenants",
        "/apps/..#",
        "/apps/..?x",
        "/apps/x?",
        "/apps/..%2f..%2ftenants",
        "/apps/x\n",
    ):
        assert not sent_as_is(path), path


async def test_expiry_does_not_start_a_suspend(world: World) -> None:
    """到期不另外发起停用：用户中心把到期算作挡住（登录不了、取权限一律无权限）。"""
    c = await admin_console(world.ops)
    tid = (await _open(c))["tenant"]["id"]
    await _settle(world)
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    assert (await c.send("PATCH", f"/api/tenants/{tid}", {"expires_at": past})).status_code == 200
    detail = await _uc_tenant(c, tid)
    assert detail["tenant"]["usable"] is False
    assert [o["kind"] for o in detail["operations"]] == ["OPEN"]
    await c.close()
