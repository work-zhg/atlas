"""服务器上的测试数据：部门、测试用户（统一密码）、角色，以及一个能把活派给 ACP SubAgent 的智能体。

    SEED_PASSWORD='…' UC_ADMIN_PASSWORD='…' uv run python deploy/server/seed_test.py

★ 部门、账号、姓名、邮箱、角色照本机用户中心的测试数据复刻；另外每人至少有 Atlas「构建者」与
  TeamFlow「团队成员」，登录就能建智能体、跑流程。外加评测账号 atlas_bench（部署后冒烟用；
  账号只许小写字母、数字、点、下划线，所以不是 atlas-bench）。
★ 全部经网关的公开接口做（用户中心记审计、Atlas 记创建人），不直接写库。可重复执行：
  已有的部门 / 用户 / 授权 / 智能体跳过；已有用户的密码不是 SEED_PASSWORD 时重置成它。
★ 密码只从环境变量读，不写进文件。
"""

from __future__ import annotations

import os
import sys

import httpx

GATEWAY = os.environ.get("ATLAS_GATEWAY", "http://atlas.192.168.0.112.nip.io")
UC = f"{GATEWAY}/uc/api/v1"
ATLAS = f"{GATEWAY}/atlas-api/v1"
DOMAIN = "xinghai.com"

#: 部门 → 上级部门（根部门是 bootstrap 建的「星海科技」）
DEPTS = {
    "产品部": "星海科技",
    "人力资源部": "星海科技",
    "研发中心": "星海科技",
    "交易中台": "研发中心",
    "技术中心": "研发中心",
    "质量部": "研发中心",
}
#: 账号 → (姓名, 部门, 本机上的角色)
USERS = {
    "chen.yun": ("陈运", "技术中心", ["TF_TEAM_ADMIN"]),
    "fang.yuan": ("方圆", "人力资源部", []),
    "gao.feng": ("高峰", "技术中心", []),
    "he.chao": ("何超", "交易中台", []),
    "li.gong": ("李工", "交易中台", ["TF_TEAM_ADMIN", "UC_USER_ADMIN"]),
    "lin.yuan": ("林远", "星海科技", ["UC_AUDITOR"]),
    "wang.ce": ("王测", "质量部", []),
    "xu.qing": ("许晴", "交易中台", []),
    "zhang.ming": ("张明", "产品部", ["ATLAS_ADMIN", "SKILL_ADMIN", "TF_PLATFORM_ADMIN"]),
    "zhou.ning": ("周宁", "产品部", []),
    # 评测账号：deploy/local/smoke.sh 与 atlas_bench 用它（.env 的 ATLAS_BENCH_UID）
    "atlas_bench": ("评测账号", "研发中心", []),
}
#: 没有更高角色时补上的基线：能登录就能用
BASELINE = {
    "atlas": ("ATLAS_BUILDER", {"ATLAS_ADMIN"}),
    # 技能应用（用户中心里的独立应用）：构建者原来在 Atlas 名下就能上传技能
    "skill": ("SKILL_AUTHOR", {"SKILL_ADMIN"}),
    "teamflow": ("TF_TEAM_MEMBER", {"TF_TEAM_ADMIN", "TF_PLATFORM_ADMIN"}),
}

AGENT_SLUG = "acp-delegate"
AGENT = {
    "slug": AGENT_SLUG,
    "name": "ACP 委派测试",
    "description": "原生协调者：动手的活（写代码、改文件、跑命令）派给 Claude Code（ACP）SubAgent",
    "spec": {
        # kind 不写 = native。★ 只有 native 能派 SubAgent：ACP 的工具面由 CLI 自带
        "system_prompt": (
            "你是协调者。需要在工作区里动手的活（写代码、改文件、跑命令、git 操作），"
            "一律用 task 工具派给 SubAgent claude-code，不要自己写文件。"
            "它完成后，用 filesystem 工具读回结果核对，再向用户简要汇报做了什么、产物在哪。"
            "纯问答、解释、规划类的问题直接回答，不必派活。"
        ),
        "model": {"model": "deepseek-v4-pro", "provider": "anthropic"},
        "tool_names": ["task", "filesystem", "write_todos"],
        "subagents": [
            {
                "name": "claude-code",
                "description": (
                    "在独立的会话 Pod 里用 Claude Code 动手：写代码、改文件、跑命令、装依赖。"
                    "与你共享同一个工作区（/workspace），它写的文件你能直接读到"
                ),
                "system_prompt": (
                    "你在会话 Pod 里，工作区是当前目录（/workspace）。按要求完成任务，"
                    "完成后简要说明做了什么、改了哪些文件。git 仓库放 /repos，不要放进 /workspace。"
                ),
                "model": {"model": "deepseek-v4-pro", "provider": "anthropic"},
                "kind": "acp",
                "cli": {
                    "cli_type": "claude-code",
                    "adapter": "node /opt/acp-cli/bin/claude-acp",
                    "image": "atlas-acp-bridge:0.4.0",
                },
            }
        ],
    },
}


class Session:
    """一个登录会话（网关的 uc_session Cookie）。写请求带 CSRF 头。"""

    def __init__(self, account: str, password: str) -> None:
        self.c = httpx.Client(trust_env=False, timeout=30)
        r = self.c.post(f"{UC}/auth/login", json={"account": account, "password": password})
        self.ok = r.status_code == 200
        self.must_change = bool(self.ok and r.json().get("must_change_password"))

    def _h(self) -> dict[str, str]:
        return {"X-UC-CSRF": self.c.cookies.get("uc_csrf") or ""}

    def get(self, url: str, **params: object) -> object:
        r = self.c.get(url, params=params)
        r.raise_for_status()
        return r.json()

    def send(self, method: str, url: str, body: object) -> object:
        r = self.c.request(method, url, json=body, headers=self._h())
        if r.status_code >= 400:
            sys.exit(f"✗ {method} {url} → {r.status_code}：{r.text[:300]}")
        return r.json() if r.content else None


def set_password(account: str, current: str, new: str) -> None:
    user = Session(account, current)
    if not user.ok:
        sys.exit(f"✗ {account} 用临时密码登录失败")
    user.send("POST", f"{UC}/auth/password", {"old_password": current, "new_password": new})


def main() -> int:
    seed = os.environ.get("SEED_PASSWORD") or sys.exit("缺 SEED_PASSWORD")
    admin_pw = os.environ.get("UC_ADMIN_PASSWORD") or sys.exit("缺 UC_ADMIN_PASSWORD")
    admin = Session("admin", admin_pw)
    if not admin.ok or admin.must_change:
        sys.exit("✗ admin 登录失败或还没改过临时密码")

    # ── 部门 ──
    depts = {d["name"]: d["id"] for d in admin.get(f"{UC}/depts/tree")}
    for name, parent in DEPTS.items():
        if name not in depts:
            created = admin.send("POST", f"{UC}/depts", {"parent_id": depts[parent], "name": name})
            depts[name] = created["id"]
            print(f"✓ 部门 {parent}/{name}")

    # ── 角色 ──
    roles: dict[str, str] = {}
    for app in admin.get(f"{UC}/apps"):
        for role in admin.get(f"{UC}/apps/{app['id']}/roles"):
            roles[role["code"]] = role["id"]

    # ── 用户 ──
    for account, (name, dept, own_roles) in USERS.items():
        found = [u for u in admin.get(f"{UC}/users", q=account)["items"] if u["account"] == account]
        if found:
            uid = found[0]["id"]
            if not Session(account, seed).ok:
                temp = admin.send("POST", f"{UC}/users/{uid}/reset-password", {})["temp_password"]
                set_password(account, temp, seed)
                print(f"✓ {account} 已存在，密码重置")
        else:
            body = {
                "name": name,
                "account": account,
                "email": f"{account}@{DOMAIN}",
                "dept_id": depts[dept],
            }
            created = admin.send("POST", f"{UC}/users", body)
            uid = created["user"]["id"]
            set_password(account, created["temp_password"], seed)
            print(f"✓ 用户 {account}（{name}，{dept}）")
        wanted = list(own_roles)
        for base, higher in BASELINE.values():
            if not higher & set(own_roles) and base not in own_roles:
                wanted.append(base)
        res = admin.send(
            "POST",
            f"{UC}/grants",
            {
                "kind": "role",
                "target_ids": [roles[c] for c in wanted],
                "subjects": [{"type": "user", "id": uid}],
            },
        )
        print(f"  角色 {', '.join(wanted)}（新增 {res['added']}）")

    # ── Atlas 智能体（建在 admin 名下；P1 里所有人都能用所有智能体）──
    agents = admin.get(f"{ATLAS}/agents", q=AGENT_SLUG, limit=50)["data"]
    if any(a["slug"] == AGENT_SLUG for a in agents):
        print(f"智能体 {AGENT_SLUG} 已存在")
    else:
        created = admin.send("POST", f"{ATLAS}/agents", AGENT)
        print(f"✓ 智能体「{AGENT['name']}」（{AGENT_SLUG}，id {created['id']}）")
    bench = [
        u
        for u in admin.get(f"{UC}/users", q="atlas_bench")["items"]
        if u["account"] == "atlas_bench"
    ]
    if bench:
        print(f"评测账号 atlas_bench 的 uid：{bench[0]['id']}（冒烟时设 ATLAS_BENCH_UID）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
