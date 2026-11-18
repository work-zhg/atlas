"""管理台改版用到的接口：应用入口地址、结构化密码规则、本人的登录记录。"""

from __future__ import annotations

from uc_testkit import STRONG, Client, admin_client, make_user, root_id


async def test_app_home_url_validated_and_listed_in_my_apps() -> None:
    c = await admin_client()
    # 挡住 javascript: 与协议相对地址（// 开头会跳到别的域名）
    for bad in ("javascript:alert(1)", "//evil.example.com", "crm"):
        r = await c.post("/api/v1/apps", {"name": f"坏地址-{bad}", "home_url": bad})
        assert r.status_code == 422 and r.json()["code"] == "APP_HOME_URL_INVALID", bad
    r = await c.post("/api/v1/apps", {"name": "客户管理", "home_url": "/crm"})
    assert r.status_code == 201, r.text
    app = r.json()["app"]
    assert app["home_url"] == "/crm"
    # 默认无人可访问；开放给全员后出现在「我的应用」，带入口地址
    assert all(a["id"] != app["id"] for a in (await c.get("/api/v1/me/apps")).json())
    await c.put(f"/api/v1/apps/{app['id']}/scope", {"all": True})
    mine = {a["id"]: a for a in (await c.get("/api/v1/me/apps")).json()}
    assert mine[app["id"]]["home_url"] == "/crm"
    # 改成完整地址；清空 = 不提供入口
    r = await c.patch(f"/api/v1/apps/{app['id']}", {"home_url": "https://crm.xinghai.com"})
    assert r.json()["home_url"] == "https://crm.xinghai.com"
    r = await c.patch(f"/api/v1/apps/{app['id']}", {"home_url": "  "})
    assert r.json()["home_url"] is None
    await c.close()


async def test_me_has_structured_password_rules() -> None:
    c = await admin_client()
    me = (await c.get("/api/v1/auth/me")).json()
    # 与默认配置一致：至少 8 位、含大写、含数字、不要求符号
    assert me["password_rules"] == {
        "min_len": 8,
        "need_upper": True,
        "need_digit": True,
        "need_symbol": False,
    }
    assert me["password_policy"] == "至少 8 位、含大写字母、含数字"
    await c.close()


async def test_my_login_logs_only_mine_including_failures() -> None:
    admin = await admin_client()
    _uid, temp = await make_user(admin, "li.si", await root_id(admin))
    c = Client()
    r = await c.login("li.si", "wrong-password", tenant="default")
    assert r.status_code == 401
    await c.login_and_activate("li.si", temp, tenant="default")
    logs = (await c.get("/api/v1/me/login-logs")).json()
    # 最新的在前：成功的那次 + 之前输错的那次；管理员的登录不在其中
    assert [x["ok"] for x in logs] == [True, False]
    assert logs[1]["reason"] and logs[0]["time"] >= logs[1]["time"]
    assert set(logs[0]) == {"time", "ip", "ok", "reason", "app"}
    # 改密后用新密码也能看
    c2 = Client()
    assert (await c2.login("li.si", STRONG, tenant="default")).status_code == 200
    assert len((await c2.get("/api/v1/me/login-logs")).json()) == 3
    for x in (admin, c, c2):
        await x.close()
