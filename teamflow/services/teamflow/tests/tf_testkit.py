"""测试工具：一个世界（内存用户中心 + Atlas + 应用），多个浏览器客户端。"""

from __future__ import annotations

from typing import Any

import httpx
from atlas_teamflow.api.app import create_app
from atlas_teamflow.identity import snapshots
from atlas_teamflow.settings import get_settings
from tf_fakes import FakeAtlas, FakeUC, MemoryRepoHost


class Client:
    """模拟经网关来的浏览器：网关校验过用户中心登录态，把身份写进请求头（内网身份传递 §4）。"""

    def __init__(self, app: Any, uid: str | None = None) -> None:
        headers = {"X-Caller-App": "gateway", **({"X-Uid": uid} if uid else {})}
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://tf", headers=headers
        )

    def _h(self) -> dict[str, str]:
        return {}

    async def get(self, url: str, **kw: Any) -> httpx.Response:
        return await self.http.get(url, **kw)

    async def post(self, url: str, json: Any = None, **kw: Any) -> httpx.Response:
        return await self.http.post(
            url, json=json, headers={**self._h(), **kw.pop("headers", {})}, **kw
        )

    async def patch(self, url: str, json: Any = None) -> httpx.Response:
        return await self.http.patch(url, json=json, headers=self._h())

    async def put(self, url: str, json: Any = None) -> httpx.Response:
        return await self.http.put(url, json=json, headers=self._h())

    async def delete(self, url: str, **kw: Any) -> httpx.Response:
        return await self.http.delete(url, headers=self._h(), **kw)


class World:
    def __init__(self, coord: Any = None, oauth: Any = None, **overrides: Any) -> None:
        self.uc = FakeUC()
        self.atlas = FakeAtlas()
        self.repo = MemoryRepoHost()
        self.atlas.host = self.repo
        self.settings = get_settings().model_copy(update={"team_level_ttl_seconds": 0, **overrides})
        snapshots.clear()  # 授权快照是进程内缓存：每个世界从头开始
        self.app = create_app(
            self.settings,
            uc=self.uc,  # type: ignore[arg-type]
            atlas=self.atlas,  # type: ignore[arg-type]
            coord=coord,
            repo=self.repo,
            oauth=oauth,
        )
        # 平台管理员、两位团队管理员、三位成员
        self.ids = {
            "padmin": self.uc.add_user("padmin", "平台管理员", ["TF_PLATFORM_ADMIN"]),
            "lead": self.uc.add_user("lead", "李工", ["TF_TEAM_ADMIN", "TF_TEAM_MEMBER"]),
            "lead2": self.uc.add_user("lead2", "周工", ["TF_TEAM_ADMIN", "TF_TEAM_MEMBER"]),
            "dev": self.uc.add_user("dev", "王工", ["TF_TEAM_MEMBER"]),
            "qa": self.uc.add_user("qa", "赵工", ["TF_TEAM_MEMBER"]),
            "outsider": self.uc.add_user("outsider", "外人", ["TF_TEAM_MEMBER"]),
        }
        # 平台管理员管全部团队：数据权限（用户中心的「全部团队 · Owner」），不在角色里
        self.uc.grant_all_teams(self.ids["padmin"], "OWNER")

    async def settle(self) -> None:
        """等 Agent 监督器把排队的指令都投递完、回复都落库。"""
        if self.app.state.supervisor:
            await self.app.state.supervisor.idle()

    def id(self, account: str) -> str:
        return str(self.ids[account])

    async def as_(self, account: str) -> Client:
        c = Client(self.app, self.id(account))
        r = await c.get("/api/v1/auth/me")
        assert r.status_code == 200, r.text
        return c


def ok(r: httpx.Response, status: int = 200) -> Any:
    assert r.status_code == status, f"{r.status_code} {r.text}"
    return r.json()


def node(
    name: str, exec_role: str, output: str, review: str, admit: str | None = None
) -> dict[str, Any]:
    return {
        "name": name,
        "exec_role": exec_role,
        "output_name": output,
        "artifact_file_template_id": None,
        "exit_review": {"role": review, "rule": "any"},
        "admit_review": {"role": admit, "rule": "all"} if admit else None,
    }


def simple_definition() -> dict[str, Any]:
    """需求 → (设计 ∥ 测试方案) → 开发。准入配在下游节点上（开工前审核上游产物）。"""
    return {
        "flow": [
            {"type": "node", "id": "n1"},
            {"type": "parallel", "tracks": [{"nodes": ["n2"]}, {"nodes": ["n3"]}]},
            {"type": "node", "id": "n4"},
        ],
        "nodes": {
            "n1": node("需求分析", "产品", "需求文档", "产品评审"),
            "n2": node("架构设计", "架构", "设计文档", "技术评审", admit="技术评审"),
            "n3": node("测试方案", "测试", "测试方案", "技术评审", admit="技术评审"),
            "n4": node("开发", "开发", "代码", "技术评审", admit="技术评审"),
        },
    }


async def published_template(c: Client, name: str = "标准研发流程") -> dict[str, Any]:
    t = ok(await c.post("/api/v1/flow-templates", {"name": name}), 201)
    d = t["draft"]
    ok(
        await c.put(
            f"/api/v1/flow-templates/{t['id']}/draft",
            {"definition": simple_definition(), "version": d["version"]},
        )
    )
    return ok(await c.post(f"/api/v1/flow-templates/{t['id']}/publish", {"change_note": "首版"}))
