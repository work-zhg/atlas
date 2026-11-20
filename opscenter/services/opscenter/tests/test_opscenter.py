"""运营中心（c4-model/opscenter/）：运营账号、租户的开通 / 停用 / 恢复 / 关闭、系统代理、到期。

账号     账号密码登录，临时密码必须先改；连续输错锁定；观察员只能看；最后一个运营管理员不能停用
开通     第一步在接口里同步跑，临时密码只在响应里；之后的步骤后台推进（设份额）
停用     先挡住用户中心的入口，再按登记顺序挂起各系统；失败退避重试，可以立即重做
关闭     只能关已停用的、企业代码要输对；各系统分批删到没有剩，最后删用户中心里的租户
代理     只放行白名单（应用目录、份额…）；建租户、挡住、删租户只能经租户操作
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from atlas_opscenter.db.models import AuditEntry, OperationStep
from atlas_opscenter.db.session import get_sessionmaker
from conftest import STRONG, Console, World, admin_console
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


# ───────────────────────────── 运营账号


async def test_login_requires_changing_the_temp_password_and_locks_after_failures(
    world: World,
) -> None:
    from atlas_opscenter.accounts import Accounts
    from atlas_opscenter.settings import get_settings

    async with get_sessionmaker()() as s:
        temp = await Accounts(s, get_settings()).bootstrap("ops", "值班")
        await s.commit()
        # 只能 bootstrap 一次
        try:
            await Accounts(s, get_settings()).bootstrap("ops2", "另一个")
            raise AssertionError("第二次 bootstrap 应该被拒")
        except Exception as exc:
            assert "bootstrap" in str(exc)
    c = Console(world.ops)
    assert (await c.login("ops", temp)).json()["must_change_password"] is True
    r = await c.get("/api/tenants")
    assert r.status_code == 403 and r.json()["code"] == "PASSWORD_CHANGE_REQUIRED"
    r = await c.post("/api/auth/password", {"old_password": temp, "new_password": "short"})
    assert r.json()["code"] == "PASSWORD_POLICY"
    assert (
        await c.post("/api/auth/password", {"old_password": temp, "new_password": STRONG})
    ).status_code == 200
    assert (await c.get("/api/auth/me")).json()["must_change_password"] is False

    bad = Console(world.ops)
    for _ in range(5):
        assert (await bad.login("ops", "wrong")).status_code == 401
    r = await bad.login("ops", STRONG)
    assert r.json()["code"] == "ACCOUNT_LOCKED"
    # 写请求要 CSRF 头
    r = await c.http.post("/api/operators", json={"account": "x.y", "name": "x"})
    assert r.json()["code"] == "CSRF_FAILED"
    for x in (c, bad):
        await x.close()


async def test_observers_only_read_and_the_last_admin_stays(world: World) -> None:
    admin = await admin_console(world.ops)
    r = await admin.post("/api/operators", {"account": "li.si", "name": "李四", "role": "OBSERVER"})
    assert r.status_code == 201, r.text
    observer_id, temp = r.json()["operator"]["id"], r.json()["temp_password"]
    obs = Console(world.ops)
    await obs.login("li.si", temp)
    await obs.post("/api/auth/password", {"old_password": temp, "new_password": STRONG})
    assert (await obs.get("/api/tenants")).status_code == 200
    r = await obs.post("/api/tenants/00000000-0000-0000-0000-000000000001/suspend", {})
    assert r.json()["code"] == "ADMIN_REQUIRED"
    r = await obs.send("PUT", "/api/systems/alpha/ops/quotas/default", {"storage_bytes": 1})
    assert r.json()["code"] == "ADMIN_REQUIRED"

    me = (await admin.get("/api/auth/me")).json()["id"]
    r = await admin.send("PATCH", f"/api/operators/{me}", {"role": "OBSERVER"})
    assert r.json()["code"] == "LAST_ADMIN"
    # 停用后他的登录立即作废
    assert (await admin.post(f"/api/operators/{observer_id}/disable")).status_code == 200
    assert (await obs.get("/api/tenants")).status_code == 401
    for x in (admin, obs):
        await x.close()


# ───────────────────────────── 开通


async def test_open_hands_over_the_temp_password_once_then_sets_quotas(world: World) -> None:
    c = await admin_console(world.ops)
    quota = {"running_runs": 20, "sandboxes": 30, "storage_bytes": 1024}
    out = await _open(c, quotas={"alpha": quota})
    assert out["temp_password"] and out["admin"]["account"] == "wangwu"
    tid = out["tenant"]["id"]
    assert [s["status"] for s in out["operation"]["steps"]] == ["DONE", "PENDING"]

    await _settle(world)
    assert world.alpha.quotas[tid] == quota
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


# ───────────────────────────── 停用、恢复、关闭


async def test_suspend_blocks_the_entry_first_and_retries_a_failing_system(world: World) -> None:
    c = await admin_console(world.ops)
    tid = (await _open(c))["tenant"]["id"]
    await _settle(world)
    world.beta.fail["suspend"] = 1

    r = await c.post(f"/api/tenants/{tid}/suspend", {"reason": "欠费"})
    assert r.status_code == 202, r.text
    op_id = r.json()["id"]
    assert [(s["system"], s["action"]) for s in r.json()["steps"]] == [
        ("usercenter", "BLOCK"),
        ("alpha", "SUSPEND"),
        ("beta", "SUSPEND"),
    ]
    # 有进行中的操作时不能再发起
    assert (await c.post(f"/api/tenants/{tid}/resume")).json()["code"] == "OPERATION_RUNNING"

    await _settle(world, 3)
    op = (await _uc_tenant(c, tid))["operations"][0]
    assert op["status"] == "RUNNING" and op["stuck"] is True
    failed = next(s for s in op["steps"] if s["status"] == "FAILED")
    assert failed["system"] == "beta" and "暂时不可用" in failed["last_error"]
    assert (await _uc_tenant(c, tid))["tenant"]["status"] == "suspended", "入口先挡住了"

    # 立即重做：不等退避
    assert (await c.post(f"/api/operations/{op_id}/retry")).status_code == 200
    await _settle(world)
    assert (await _uc_tenant(c, tid))["operations"][0]["status"] == "DONE"
    assert [a for a, _ in world.beta.calls] == ["suspend", "suspend"]
    # 重做用的是同一个幂等键
    assert world.beta.idempotency[0] == world.beta.idempotency[1]

    r = await c.post(f"/api/tenants/{tid}/resume")
    assert [(s["system"], s["action"]) for s in r.json()["steps"]] == [
        ("alpha", "RESUME"),
        ("beta", "RESUME"),
        ("usercenter", "UNBLOCK"),
    ]
    await _settle(world)
    assert (await _uc_tenant(c, tid))["tenant"]["status"] == "active"
    await c.close()


async def test_close_only_a_suspended_tenant_and_purge_until_nothing_is_left(world: World) -> None:
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
    await _settle(world)
    assert [a for a, _ in world.alpha.calls].count("purge") == 3
    # 用户中心最后删：租户没了，运营库里的操作记录还在（带企业代码快照）
    assert (await c.get(f"/api/tenants/{tid}")).status_code == 404
    async with get_sessionmaker()() as s:
        steps = list(await s.scalars(select(OperationStep).where(OperationStep.action == "PURGE")))
    alpha_step = next(st for st in steps if st.system == "alpha")
    assert alpha_step.status == "DONE" and alpha_step.result == {"deleted": {"rows": 30}}
    await c.close()


async def test_a_rejected_step_backs_off_longer_and_is_marked(world: World) -> None:
    c = await admin_console(world.ops)
    tid = (await _open(c))["tenant"]["id"]
    await _settle(world)
    world.alpha.reject.add("suspend")
    await c.post(f"/api/tenants/{tid}/suspend", {})
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


# ───────────────────────────── 系统代理、到期


async def test_proxy_passes_the_allowlist_only_and_audits_writes(world: World) -> None:
    c = await admin_console(world.ops)
    systems = {s["code"]: s for s in (await c.get("/api/systems")).json()}
    assert systems["alpha"]["capabilities"] == ["capacity", "lifecycle"]
    r = await c.get("/api/systems/usercenter/ops/apps")
    assert r.status_code == 200 and any(a["is_builtin"] for a in r.json())
    r = await c.post("/api/systems/usercenter/ops/apps", {"name": "客户管理", "code": "crm"})
    assert r.status_code == 201 and r.json()["app_secret"]
    assert (await c.get("/api/systems/alpha/ops/capacity")).json() == {
        "usage": {"storage_bytes": 1}
    }
    # 建租户、挂起、删除不能从代理绕过租户操作
    for method, path in (
        ("POST", "/api/systems/usercenter/ops/tenants"),
        ("DELETE", "/api/systems/usercenter/ops/tenants/00000000-0000-0000-0000-000000000001"),
        ("POST", "/api/systems/alpha/ops/tenants/00000000-0000-0000-0000-000000000001/suspend"),
    ):
        r = await c.send(method, path, {})
        assert r.status_code == 403 and r.json()["code"] == "NOT_ALLOWED", (method, path)
    async with get_sessionmaker()() as s:
        rows = list(await s.scalars(select(AuditEntry).where(AuditEntry.action == "system.post")))
    assert [r.target for r in rows] == ["usercenter /apps"]
    assert "app_secret" not in str(rows[0].detail)
    await c.close()


async def test_expired_tenants_are_suspended_automatically(world: World) -> None:
    c = await admin_console(world.ops)
    tid = (await _open(c))["tenant"]["id"]
    await _settle(world)
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    assert (await c.send("PATCH", f"/api/tenants/{tid}", {"expires_at": past})).status_code == 200
    assert await world.expiry.tick() == 1
    assert await world.expiry.tick() == 0, "已有进行中的操作：不重复发起"
    await _settle(world)
    op = (await _uc_tenant(c, tid))["operations"][0]
    assert op["kind"] == "SUSPEND" and op["reason"] == "到期" and op["status"] == "DONE"
    assert op["requested_by"] == "运营中心"
    await c.close()
