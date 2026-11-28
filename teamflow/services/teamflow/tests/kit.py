"""测试工具：一个世界（内存用户中心 + Agent 平台 + 应用），多个浏览器客户端。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
from atlas_teamflow.api.app import create_app
from atlas_teamflow.settings import get_settings
from atlas_teamflow.usercenter import MARKET_CODE
from fakes import TEAM_ADMIN_OPS, TEAM_MEMBER_OPS, FakeAtlas, FakeUC


def ok(r: httpx.Response, status: int = 200) -> Any:
    assert r.status_code == status, f"{r.status_code} {r.text}"
    return r.json()


class Client:
    """经网关来的浏览器：网关验过登录态，把用户和租户写进请求头。"""

    def __init__(self, app: Any, uid: UUID, tenant: UUID) -> None:
        self.uid = uid
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://tf",
            headers={"X-Uid": str(uid), "X-Tenant-Id": str(tenant), "X-Caller-App": "gateway"},
        )

    async def get(self, url: str, **kw: Any) -> httpx.Response:
        return await self.http.get(url, **kw)

    async def post(self, url: str, json: Any = None, **kw: Any) -> httpx.Response:
        return await self.http.post(url, json=json, **kw)

    async def put(self, url: str, json: Any = None, **kw: Any) -> httpx.Response:
        return await self.http.put(url, json=json, **kw)

    async def patch(self, url: str, json: Any = None) -> httpx.Response:
        return await self.http.patch(url, json=json)

    async def delete(self, url: str) -> httpx.Response:
        return await self.http.delete(url)


class World:
    """一个租户（星海科技）+ 系统租户。李工是团队管理员，周工、陈运是成员，何总只读；
    林运营在系统租户里维护市场。"""

    def __init__(self) -> None:
        self.uc = FakeUC()
        self.atlas = FakeAtlas()
        self.settings = get_settings().model_copy(update={"authz_ttl_seconds": 0})
        self.app = create_app(self.settings, uc=self.uc, atlas=self.atlas)  # type: ignore[arg-type]
        self.tenant = self.uc.add_tenant("星海科技")
        self.system = self.uc.add_tenant("平台运营", system=True)
        self.ids = {
            "li": self.uc.add_user("li", "李工", self.tenant, TEAM_ADMIN_OPS),
            "zhou": self.uc.add_user("zhou", "周工", self.tenant, TEAM_MEMBER_OPS),
            "chen": self.uc.add_user("chen", "陈运", self.tenant, TEAM_MEMBER_OPS),
            "he": self.uc.add_user("he", "何总", self.tenant, TEAM_MEMBER_OPS),
            "lin": self.uc.add_user("lin", "林运营", self.system, TEAM_MEMBER_OPS),
        }
        self.uc.grant_type(MARKET_CODE, self.ids["lin"], "WRITE")

    def as_(self, account: str) -> Client:
        tenant = self.system if account == "lin" else self.tenant
        return Client(self.app, self.ids[account], tenant)

    async def settle(self) -> None:
        """等投递器把排着的指令都投完。"""
        await self.app.state.dispatcher.drain()


def structure() -> dict[str, Any]:
    """需求澄清 → PRD →（编码 ∥ 测试用例）。产品执行前两个节点（共用一个模板角色）；
    编码、测试用例的准入由开发审。"""

    def node(
        name: str, role: str, exit_roles: list[str], admit: list[str] | None = None
    ) -> dict[str, Any]:
        return {
            "name": name,
            "instructions": f"做好{name}",
            "exec_role": role,
            "admit": {"roles": admit, "rule": "any"} if admit else None,
            "exit": {"roles": exit_roles, "rule": "any"},
        }

    return {
        "roles": {"pm": "产品", "dev": "开发", "qa": "测试"},
        "flow": [
            {"type": "node", "id": "clarify"},
            {"type": "node", "id": "prd"},
            {"type": "parallel", "tracks": [{"nodes": ["code"]}, {"nodes": ["cases"]}]},
        ],
        "nodes": {
            "clarify": node("需求澄清", "pm", ["pm"]),
            "prd": node("PRD", "pm", ["pm"]),
            "code": node("编码", "dev", ["dev"], admit=["dev"]),
            "cases": node("测试用例", "qa", ["qa"], admit=["qa"]),
        },
    }


async def setup_project(w: World) -> dict[str, Any]:
    """团队 + 三个数字员工（李工是 Owner）+ 发布的模板 + 项目，角色配齐：
    产品 = 周工 + 需求分析师，开发 = 李工、周工 + 开发工程师，测试 = 陈运 + 测试工程师。"""
    li = w.as_("li")
    team = ok(
        await li.post(
            "/api/v1/teams",
            {
                "name": "研发一部",
                "members": [
                    {"id": str(w.ids["zhou"])},
                    {"id": str(w.ids["chen"])},
                    {"id": str(w.ids["he"]), "level": "READ"},
                ],
            },
        ),
        201,
    )
    agents = {}
    for key, name in (("pm", "需求分析师"), ("dev", "开发工程师"), ("qa", "测试工程师")):
        aid = w.atlas.add_agent(name, w.ids["li"])
        agents[key] = ok(
            await li.post(f"/api/v1/teams/{team['id']}/agents", {"atlas_agent_id": aid}), 201
        )
    tpl = ok(
        await li.post(f"/api/v1/teams/{team['id']}/templates", {"name": "软件研发（标准）"}), 201
    )
    ok(
        await li.put(
            f"/api/v1/templates/{tpl['id']}/draft",
            {"structure": structure(), "rev": tpl["draft"]["rev"]},
        )
    )
    ok(await li.post(f"/api/v1/templates/{tpl['id']}/publish", {"note": "首版"}), 201)
    project = ok(await li.post(f"/api/v1/teams/{team['id']}/projects", {"name": "支付中台"}), 201)
    ok(
        await li.post(f"/api/v1/projects/{project['id']}/templates", {"template_id": tpl["id"]}),
        201,
    )
    for role, members, agent in (
        ("pm", ["zhou"], "pm"),
        ("dev", ["li", "zhou"], "dev"),
        ("qa", ["chen"], "qa"),
    ):
        ok(
            await li.put(
                f"/api/v1/projects/{project['id']}/templates/{tpl['id']}/roles/{role}",
                {"members": [str(w.ids[m]) for m in members], "agent": agents[agent]["id"]},
            )
        )
    return {"team": team, "agents": agents, "template": tpl, "project": project}
