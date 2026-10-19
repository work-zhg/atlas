"""TeamFlow 在用户中心登记的操作、菜单、角色、数据编码（权限设计 §04–§07）。

`python -m atlas_teamflow register-uc` 按本目录幂等同步到用户中心。
"""

from __future__ import annotations

from typing import Any

APP_NAME = "AI TeamFlow"
#: 应用编码：内网调用用户中心开放接口、Atlas 时 X-Caller-App 写它（内网身份传递 §3）
APP_CODE = "teamflow"

#: (模块, 编码, 名称)
OPERATIONS: list[tuple[str, str, str]] = [
    ("流程模板", "flow_template:view", "查看流程模板"),
    ("流程模板", "flow_template:manage", "维护流程模板"),
    ("文件模板", "file_template:view", "查看文件模板"),
    ("文件模板", "file_template:manage", "维护文件模板"),
    ("团队", "team:create", "新建团队"),
    ("团队", "team:edit", "编辑团队信息"),
    ("团队", "team:member", "管理团队成员"),
    ("团队", "team:agent", "接入 / 移除团队 Agent"),
    ("项目", "project:create", "新建项目"),
    ("项目", "project:configure", "项目设置"),
    ("项目", "project:archive", "归档项目"),
    ("流程", "process:view", "查看项目与流程"),
    ("流程", "process:start", "发起流程"),
    ("流程", "process:handle", "节点协同"),
    ("流程", "process:review", "准入 / 准出评审"),
    ("流程", "process:terminate", "终止流程"),
    ("系统", "settings:manage", "系统设置"),
]

#: 有序；parent 为目录编码
MENUS: list[dict[str, Any]] = [
    {"type": "menu", "code": "todo", "name": "待我处理", "icon": "📥", "path": "/todo"},
    {
        "type": "menu",
        "code": "teams",
        "name": "团队",
        "icon": "👥",
        "path": "/teams",
        "is_public": True,
    },
    # 系统设置：流程模板、文件模板与集成配置，只有平台管理员可见、可配置
    {"type": "dir", "code": "sys", "name": "系统设置", "icon": "⚙️"},
    {
        "type": "menu",
        "code": "flow_tpl",
        "name": "流程模板",
        "icon": "🧭",
        "path": "/templates/flow",
        "parent": "sys",
    },
    {
        "type": "menu",
        "code": "file_tpl",
        "name": "文件模板",
        "icon": "📄",
        "path": "/templates/file",
        "parent": "sys",
    },
    {
        "type": "menu",
        "code": "settings",
        "name": "集成配置",
        "icon": "🔌",
        "path": "/settings",
        "parent": "sys",
    },
]

ROLES: list[dict[str, Any]] = [
    {
        "code": "TF_PLATFORM_ADMIN",
        "name": "平台管理员",
        "description": "系统设置（流程模板、文件模板、集成配置）；新建团队、调整任意团队的管理员",
        "operations": [
            "flow_template:view",
            "flow_template:manage",
            "file_template:view",
            "file_template:manage",
            "team:create",
            # 调整任意团队的成员与管理员 = 本操作码 ∧ 对该团队 Owner。「对全部团队 Owner」
            # 是数据权限（用户中心的数据编码级授权「全部团队 · Owner」），不在角色里
            "team:member",
            "settings:manage",
        ],
        "menus": ["sys", "flow_tpl", "file_tpl", "settings"],
    },
    {
        "code": "TF_TEAM_ADMIN",
        "name": "团队管理员",
        "description": "配置团队的项目；管理团队成员与 Agent；编辑团队信息（还须是该团队的 Owner）",
        "operations": [
            "team:edit",
            "team:member",
            "team:agent",
            "project:create",
            "project:configure",
            "project:archive",
            # 只读：建项目 / 项目设置里选择流程模板（模板的配置只在系统设置，平台管理员）
            "flow_template:view",
            "file_template:view",
            "process:view",
        ],
        "menus": [],
    },
    {
        "code": "TF_TEAM_MEMBER",
        "name": "团队成员",
        "description": "处理流程：发起、节点协同、准入 / 准出评审（还须对该团队读写）",
        "operations": [
            "process:view",
            "process:start",
            "process:handle",
            "process:review",
            "process:terminate",
            "file_template:view",
        ],
        "menus": ["todo"],
    },
]

DATA_TYPE = {
    "code": "Team",
    "name": "团队",
    "description": "一个团队一条数据权限：成员读写，团队管理员 Owner",
}
