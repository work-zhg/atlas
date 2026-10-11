"""请求主体（权限设计 §4）：一次请求「是谁在做」。

每个请求解析一次，经依赖注入传给接口；service 层不允许自己去取身份（沿用原有纪律）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

__all__ = ["Principal"]


@dataclass(frozen=True)
class Principal:
    #: user = 用户中心的人（或 dev 模式的请求头身份）；service = 服务账号
    kind: Literal["user", "service"]
    #: 资源所有者 id：用户 = 用户 id；服务账号 = 服务账号 id。会话的 created_by 写它
    owner_id: UUID
    name: str = ""
    operations: frozenset[str] = field(default_factory=frozenset)
    menus: tuple[dict, ...] = ()
    #: 服务账号代表的用户（X-Atlas-Acting-User，仅在服务账号允许时）；只用于列表交集与审计
    acting_user_id: UUID | None = None
    #: 服务账号：会话的用户记忆默认值；可用智能体范围
    memory_default: Literal["on", "off"] = "on"
    agent_scope: Literal["all", "all_enabled", "list"] = "all"
    agent_ids: frozenset[UUID] = field(default_factory=frozenset)
    #: 通过 Cookie 会话认证（写请求须校验 CSRF）
    via_cookie: bool = False

    def has(self, op: str) -> bool:
        return op in self.operations

    @property
    def initiator_id(self) -> UUID | None:
        """这一轮的发起人（run.created_by）：用户本人，或服务账号代表的用户；系统投递为空。"""
        return self.owner_id if self.kind == "user" else self.acting_user_id
