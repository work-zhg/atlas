"""Atlas 在用户中心登记的操作码、菜单、角色、数据编码（权限设计 §6、§7）。

`python -m atlas_server register-uc` 按本目录幂等同步到用户中心。判定一律看操作码，
角色只是操作码的打包（用户中心里可以调整）。
"""

from __future__ import annotations

from typing import Any

APP_NAME = "Atlas"
APP_DESCRIPTION = "AI Agent 平台：智能体、对话、技能、知识库与 MCP"
APP_ICON = "🤖"

#: (模块, 编码, 名称)
OPERATIONS: list[tuple[str, str, str]] = [
    ("对话", "chat:use", "使用对话"),
    ("智能体", "agent:create", "新建智能体"),
    ("智能体", "agent:manage_all", "管理全部智能体"),
    ("技能", "skill:upload", "上传技能"),
    ("技能", "skill:review", "审核技能"),
    ("MCP", "mcp:manage", "维护 MCP 服务"),
    ("MCP", "mcp:manage_all", "管理全部 MCP 服务"),
    ("知识库", "kb:create", "新建知识库"),
    ("知识库", "kb:manage_all", "管理全部知识库"),
    ("知识库", "kb:review", "审核入库文档"),
    ("系统", "model:manage", "模型目录"),
    ("系统", "service_account:manage", "服务账号"),
    ("系统", "audit:view", "查看审计"),
]

ALL_OPERATIONS = frozenset(code for _, code, _ in OPERATIONS)

#: 有序；parent 为目录编码
MENUS: list[dict[str, Any]] = [
    {"type": "menu", "code": "chat", "name": "对话", "icon": "💬", "path": "/chat"},
    {"type": "menu", "code": "agents", "name": "智能体", "icon": "🤖", "path": "/agents"},
    {"type": "menu", "code": "memories", "name": "记忆", "icon": "🧠", "path": "/memories"},
    {"type": "menu", "code": "skills", "name": "技能", "icon": "🧩", "path": "/skills"},
    {"type": "menu", "code": "knowledge", "name": "知识库", "icon": "📚", "path": "/knowledge"},
    {"type": "menu", "code": "mcp", "name": "MCP", "icon": "🔌", "path": "/mcp"},
    {"type": "dir", "code": "sys", "name": "系统设置", "icon": "⚙️"},
    {
        "type": "menu",
        "code": "service_accounts",
        "name": "服务账号",
        "icon": "🔑",
        "path": "/settings/service-accounts",
        "parent": "sys",
    },
]

_MEMBER_OPS = ["chat:use"]
_BUILDER_OPS = [*_MEMBER_OPS, "agent:create", "skill:upload", "kb:create"]

ROLES: list[dict[str, Any]] = [
    {
        "code": "ATLAS_MEMBER",
        "name": "成员",
        "description": "使用已授权的智能体对话；查看自己的会话与记忆",
        "operations": _MEMBER_OPS,
        "menus": ["chat", "agents", "memories"],
    },
    {
        "code": "ATLAS_BUILDER",
        "name": "构建者",
        "description": "成员 + 新建智能体、上传技能、建知识库",
        "operations": _BUILDER_OPS,
        "menus": ["chat", "agents", "memories", "skills", "knowledge"],
    },
    {
        "code": "ATLAS_ADMIN",
        "name": "管理员",
        "description": "平台负责人：全部智能体与知识库、技能审核、MCP、模型目录、服务账号、审计",
        "operations": sorted(ALL_OPERATIONS),
        "menus": [
            "chat",
            "agents",
            "memories",
            "skills",
            "knowledge",
            "mcp",
            "sys",
            "service_accounts",
        ],
    },
]

#: 数据编码（权限设计 §7.1）：一个对象一条数据权限
DATA_TYPES: list[dict[str, Any]] = [
    {
        "code": "AtlasAgent",
        "name": "智能体",
        "description": "一个智能体一条：使用（READ）/ 编辑（WRITE）/ 所有者（OWNER）",
        "admin_operation_code": "agent:manage_all",
    },
    {
        "code": "AtlasMcpServer",
        "name": "MCP 服务",
        "description": "平台凭据的 MCP 服务：使用（READ）/ 所有者（OWNER）",
        "admin_operation_code": "mcp:manage_all",
    },
]
