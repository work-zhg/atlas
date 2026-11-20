"""Atlas 用到的用户中心编码：代码只拿它们做判断，不往用户中心登记。

★ 应用目录（操作码、菜单、数据编码、内置角色，以及角色含哪些操作码）由运营人员在运营中心
  录入（多租户设计）。角色和操作码的对应关系会被人调整，代码里写死就会和库里不一致，所以这里
  只有判断时用到的编码；要录哪些见 services/agent/README.md「用户中心登记」。
★ 角色归角色、数据权限归数据权限：操作码只表达「能不能做某类事」，不表达「对哪些数据」。
  「管理全部智能体 / 知识库」是数据权限 —— 在用户中心给人授该数据编码的全部数据授权
  （如「全部智能体 · Owner」）。
"""

from __future__ import annotations

#: Atlas 判断用到的全部操作码（前端菜单、知识库服务按同样的编码判断）
ALL_OPERATIONS = frozenset(
    {"chat:use", "agent:create", "kb:create", "kb:review", "settings:manage"}
)

#: 数据编码（权限设计 §7.1）：一个对象一条数据权限
AGENT_DATA = "AtlasAgent"
KNOWLEDGE_DATA = "AtlasKnowledgeBase"
#: 登录时向用户中心取「全部数据授权」级别的数据编码
DATA_TYPE_CODES: tuple[str, ...] = (AGENT_DATA, KNOWLEDGE_DATA)
