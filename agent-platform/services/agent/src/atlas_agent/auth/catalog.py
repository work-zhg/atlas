"""Atlas 在用户中心登记的操作码、菜单、角色、数据编码（权限设计 §6、§7）。

`python -m atlas_agent register-uc` 按本目录幂等同步到用户中心。判定一律看操作码，
角色只是操作码的打包（用户中心里可以调整）。

★ 角色归角色、数据权限归数据权限：操作码只表达「能不能做某类事」，不表达「对哪些数据」。
  「管理全部智能体 / MCP 服务」是数据权限 —— 在用户中心给人授该数据编码的全部数据授权
  （如「全部智能体 · Owner」），不再有 agent:manage_all、mcp:manage_all 这类操作码。
"""

from __future__ import annotations

from typing import Any

APP_NAME = "Atlas"
#: 应用编码：内网调用用户中心开放接口时 X-Caller-App 写它（内网身份传递 §3）
APP_CODE = "atlas"
APP_DESCRIPTION = "AI Agent 平台：智能体、对话、技能、知识库与 MCP"
APP_ICON = "🤖"

#: (模块, 编码, 名称)
OPERATIONS: list[tuple[str, str, str]] = [
    ("对话", "chat:use", "使用对话"),
    ("智能体", "agent:create", "新建智能体"),
    ("技能", "skill:upload", "上传技能"),
    ("技能", "skill:review", "审核技能"),
    ("MCP", "mcp:manage", "维护 MCP 服务"),
    ("知识库", "kb:create", "新建知识库"),
    ("知识库", "kb:review", "审核入库文档"),
    ("系统", "model:manage", "模型目录"),
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
        "description": (
            "平台负责人：技能审核、MCP、模型目录、审计"
            "（管理全部智能体 / 知识库另授数据权限「全部智能体 / 全部知识库 · Owner」）"
        ),
        "operations": sorted(ALL_OPERATIONS),
        "menus": [
            "chat",
            "agents",
            "memories",
            "skills",
            "knowledge",
            "mcp",
        ],
    },
]

#: 数据编码（权限设计 §7.1）：一个对象一条数据权限
DATA_TYPES: list[dict[str, Any]] = [
    {
        "code": "AtlasAgent",
        "name": "智能体",
        "description": "一个智能体一条：使用（READ）/ 编辑（WRITE）/ 所有者（OWNER）",
    },
    {
        "code": "AtlasMcpServer",
        "name": "MCP 服务",
        "description": "平台凭据的 MCP 服务：使用（READ）/ 所有者（OWNER）",
    },
    {
        "code": "AtlasKnowledgeBase",
        "name": "知识库",
        "description": (
            "一个知识库一条：检索（READ）/ 编辑文档（WRITE）/ 所有者：删除、共享（OWNER）"
        ),
    },
]

#: 退役的「管理全部」操作码 → 取代它的数据编码。register-uc 删除操作码之前，把持有它的
#: 角色授权（含岗位）换成该编码的「全部数据 · Owner」，迁移前后能管的库不变
RETIRED_ADMIN_OPS: dict[str, str] = {"kb:manage_all": "AtlasKnowledgeBase"}

#: 登录时向用户中心取「全部数据授权」级别的数据编码
DATA_TYPE_CODES: tuple[str, ...] = tuple(d["code"] for d in DATA_TYPES)
AGENT_DATA = "AtlasAgent"
MCP_DATA = "AtlasMcpServer"
KNOWLEDGE_DATA = "AtlasKnowledgeBase"
