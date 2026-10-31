"""Atlas 在用户中心登记的操作码、菜单、角色、数据编码（权限设计 §6、§7）。

`python -m atlas_agent register-uc` 按本目录幂等同步到用户中心。判定一律看操作码，
角色只是操作码的打包（用户中心里可以调整）。

★ 角色归角色、数据权限归数据权限：操作码只表达「能不能做某类事」，不表达「对哪些数据」。
  「管理全部智能体 / 知识库」是数据权限 —— 在用户中心给人授该数据编码的全部数据授权
  （如「全部智能体 · Owner」），不再有 agent:manage_all、mcp:manage_all 这类操作码。
"""

from __future__ import annotations

from typing import Any

APP_NAME = "Atlas"
#: 应用编码：内网调用用户中心开放接口时 X-Caller-App 写它（内网身份传递 §3）
APP_CODE = "atlas"
APP_DESCRIPTION = "AI Agent 平台：智能体、对话、技能、知识库"
APP_ICON = "🤖"

#: (模块, 编码, 名称)
OPERATIONS: list[tuple[str, str, str]] = [
    ("对话", "chat:use", "使用对话"),
    ("智能体", "agent:create", "新建智能体"),
    ("知识库", "kb:create", "新建知识库"),
    ("知识库", "kb:review", "审核入库文档"),
    ("系统", "model:manage", "模型目录"),
]

ALL_OPERATIONS = frozenset(code for _, code, _ in OPERATIONS)

#: 有序；parent 为目录编码
MENUS: list[dict[str, Any]] = [
    {"type": "menu", "code": "chat", "name": "对话", "icon": "💬", "path": "/chat"},
    {"type": "menu", "code": "agents", "name": "智能体", "icon": "🤖", "path": "/agents"},
    {"type": "menu", "code": "memories", "name": "记忆", "icon": "🧠", "path": "/memories"},
    {"type": "menu", "code": "knowledge", "name": "知识库", "icon": "📚", "path": "/knowledge"},
]

_MEMBER_OPS = ["chat:use"]
_BUILDER_OPS = [*_MEMBER_OPS, "agent:create", "kb:create"]

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
        "description": "成员 + 新建智能体、建知识库",
        "operations": _BUILDER_OPS,
        "menus": ["chat", "agents", "memories", "knowledge"],
    },
    {
        "code": "ATLAS_ADMIN",
        "name": "管理员",
        "description": (
            "平台负责人：模型目录、平台指令"
            "（管理全部智能体 / 知识库另授数据权限「全部智能体 / 全部知识库 · Owner」）"
        ),
        "operations": sorted(ALL_OPERATIONS),
        "menus": ["chat", "agents", "memories", "knowledge"],
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

#: 交给 MCP 网关（doc/mcp-design.html 第二版 §5.3）：MCP 的管理、「谁能用哪个 MCP 服务」都归
#: MCP 网关这个应用（用户中心里的独立应用）。register-uc 删除下面两项之前，把授权搬过去
#: （auth/register.py::_retire_mcp）；网关还没登记到用户中心时先保留，不删
MCP_GATEWAY_APP = "mcp-gateway"
#: Atlas 名下的「维护 MCP 服务」→ 网关的角色 MCP_ADMIN +「全部 MCP 服务 · 所有者」
RETIRED_MCP_OPERATION = "mcp:manage"
#: Atlas 名下的数据编码「MCP 服务」→ 网关的数据编码 McpServer（数据标识都是服务名）
RETIRED_MCP_DATA = "AtlasMcpServer"
MCP_GATEWAY_ADMIN_ROLE = "MCP_ADMIN"
MCP_GATEWAY_DATA = "McpServer"

#: 交给技能应用（用户中心里的独立应用，atlas_skill/register.py）：技能的上传、审核与审计。
#: register-uc 删除下面这几个操作码之前，把持有人搬到技能应用的角色
#: （auth/register.py::_retire_skill）；技能应用还没登记到用户中心时先保留，不删
SKILL_APP = "skill"
#: Atlas 名下退役的操作码 → 技能应用里接替它的角色。
#: audit:view 只有技能的审计在用，内置角色里持有它的只有 ATLAS_ADMIN，已随 skill:review 换成
#: SKILL_ADMIN（含 audit:view）；不单独搬，免得把「只能看审计」的人提成技能管理员
RETIRED_SKILL_OPS: dict[str, str | None] = {
    "skill:upload": "SKILL_AUTHOR",
    "skill:review": "SKILL_ADMIN",
    "audit:view": None,
}

#: 登录时向用户中心取「全部数据授权」级别的数据编码
DATA_TYPE_CODES: tuple[str, ...] = tuple(d["code"] for d in DATA_TYPES)
AGENT_DATA = "AtlasAgent"
KNOWLEDGE_DATA = "AtlasKnowledgeBase"
