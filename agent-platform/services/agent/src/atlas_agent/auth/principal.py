"""请求主体（权限设计 §4；内网身份传递 §3、§6）：一次请求「谁在用、按谁的权限判」。

每个请求解析一次，经依赖注入传给接口；service 层不允许自己去取身份（沿用原有纪律）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from uuid import UUID

__all__ = ["HUMAN_CALLERS", "LEVEL_RANK", "Principal"]

LEVEL_RANK = {"NONE": 0, "READ": 1, "WRITE": 2, "OWNER": 3}

#: 人直接操作（经网关），或来路不明（没写 X-Caller-App）：不是业务应用，不享受 §7.3 的应用信任
HUMAN_CALLERS = frozenset({"gateway", "unknown"})


@dataclass(frozen=True)
class Principal:
    #: 谁在用（X-Uid）：会话归属、用户记忆、审计里的发起人
    uid: UUID
    #: 按谁的权限判（X-Auth-Uid）：操作码、数据权限都按它。不给 = uid（人直接操作）
    auth_uid: UUID = None  # type: ignore[assignment]
    #: 直接调用方（X-Caller-App）：gateway / teamflow / bench …
    caller_app: str = "unknown"
    #: 租户（X-Tenant-Id）：原样透传给下游；后台任务没有时由用户中心按 uid 推
    tenant_id: str | None = None
    name: str = ""
    #: 角色领域：操作码（能不能做某类事）
    operations: frozenset[str] = field(default_factory=frozenset)
    menus: tuple[dict, ...] = ()
    #: 数据权限领域：数据编码 → 对该编码下**全部**数据的级别（用户中心的数据编码级授权）。
    #: ★ 与 operations 分开：「管理全部智能体」是数据权限，不经角色表达
    data_levels: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.auth_uid is None:
            object.__setattr__(self, "auth_uid", self.uid)

    def has(self, op: str) -> bool:
        return op in self.operations

    def has_all_data(self, data_code: str, level: str = "READ") -> bool:
        """对该编码下全部数据的级别是否 ≥ level（如「全部智能体 · Owner」）。"""
        return LEVEL_RANK[self.data_levels.get(data_code, "NONE")] >= LEVEL_RANK[level]

    @property
    def app(self) -> str | None:
        """调用方是业务应用（TeamFlow、评测…）时为它的编码；人直接操作为 None。

        业务应用建的会话记下它（thread.source_app），之后同一应用来访时信任它已做过授权（§7.3）。
        """
        return None if self.caller_app in HUMAN_CALLERS else self.caller_app
