"""从旧方案切到 MCP 网关的一次性迁移（MCP 方案设计 §12.4）。都可以重复执行。

    import_services  把 Atlas 原来经 MCP_SERVERS（env 方式）接入的服务登记进网关。
                     只接 Higress 上的服务（Atlas 和 Pod 只认网关，§3.1）
    grant_all_users  给根部门（含下级）授某个服务的「使用」：保持迁移前「谁都能用」
    issue_missing    给有授权但没有 Key 的人批量发 Key（§5.5、Q4）

用户中心里 Atlas 名下的操作码 mcp:manage、数据编码 AtlasMcpServer，由 Atlas 的 register-uc
退役时把授权搬到这里（atlas_agent/auth/register.py::_retire_mcp）。

★ 顺序：Higress 上的服务先改成只认网关凭据（setup.py）→ 本服务 register-uc → Atlas register-uc
  → import → 授权 → 发 Key。Higress 没改之前 import 会因「接入检查不通过」拒绝登记 —— 这正是要的。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import outbound_headers
from .errors import GatewayError
from .keys import KeyStore
from .perms import Grant, Permissions
from .registry import Registry
from .settings import GatewaySettings

logger = logging.getLogger(__name__)

__all__ = [
    "ImportReport",
    "LegacyServer",
    "grant_all_users",
    "issue_missing",
    "legacy_from_env",
    "upstream_of",
]


@dataclass(frozen=True)
class LegacyServer:
    name: str
    url: str
    call_timeout_s: float | None = None
    #: env 方式的服务默认不要求复核（它们在复核机制出现之前就在用了）
    review_required: bool = False
    enabled: bool = True


@dataclass
class ImportReport:
    imported: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    #: (服务, 原因)
    skipped: list[tuple[str, str]] = field(default_factory=list)


def upstream_of(url: str) -> str | None:
    """Higress 上 MCP 服务的地址 …/mcp-servers/<名字>[/…] → 名字；不是这种地址返回 None。"""
    parts = [p for p in urlsplit(url).path.split("/") if p]
    if "mcp-servers" not in parts:
        return None
    at = parts.index("mcp-servers")
    return parts[at + 1] if at + 1 < len(parts) else None


def legacy_from_env(items: list[dict[str, Any]]) -> list[LegacyServer]:
    """MCP_SERVERS 的 JSON（atlas-server 的环境变量）。"""
    return [
        LegacyServer(
            name=str(i["name"]),
            url=str(i["url"]),
            call_timeout_s=i.get("call_timeout_s"),
            review_required=bool(i.get("review_required", False)),
            enabled=bool(i.get("enabled", True)),
        )
        for i in items
    ]


async def import_services(
    session: AsyncSession,
    settings: GatewaySettings,
    perms: Permissions,
    servers: list[LegacyServer],
    *,
    owner: str,
) -> ImportReport:
    """一个服务一个保存点：一个失败（接入检查不通过、用户中心登记失败）不影响别的。"""
    report = ImportReport()
    registry = Registry(session, settings)
    for legacy in servers:
        if await registry.find(legacy.name) is not None:
            report.existing.append(legacy.name)
            continue
        upstream = upstream_of(legacy.url)
        if upstream is None:
            report.skipped.append((legacy.name, f"不是 Higress 上的服务（{legacy.url}）"))
            continue
        try:
            async with session.begin_nested():
                await registry.create(
                    perms,
                    name=legacy.name,
                    display_name=legacy.name,
                    description="",
                    upstream_name=upstream,
                    review_required=legacy.review_required,
                    call_timeout_s=legacy.call_timeout_s,
                    credential_mode="subject_key",
                    actor=owner,
                )
                if not legacy.enabled:
                    await registry.set_enabled(legacy.name, False, actor=owner)
        except GatewayError as exc:
            report.skipped.append((legacy.name, exc.message))
            continue
        report.imported.append(legacy.name)
        await session.flush()
    return report


async def root_dept(base_url: str) -> str:
    """用户中心的根部门（「全员」= 根部门含下级）。"""
    async with httpx.AsyncClient(timeout=10.0, trust_env=False) as http:
        r = await http.get(f"{base_url.rstrip('/')}/open/v1/depts/tree", headers=outbound_headers())
        r.raise_for_status()
        depts = r.json()
    roots = [d for d in depts if d.get("parent_id") is None]
    if not roots:
        raise GatewayError("用户中心里没有根部门")
    return str(roots[0]["id"])


async def grant_all_users(perms: Permissions, service: str, *, root: str, operator: str) -> None:
    await perms.grant(service, operator, [Grant("READ", "dept", root, include_sub=True)])


async def issue_missing(
    session: AsyncSession,
    perms: Permissions,
    services: list[str],
    *,
    actor: str,
) -> list[tuple[str, str]]:
    """给这些服务的持有人里还没有 Key 的人发 Key。→ [(uid, 名字)]。"""
    keys = KeyStore(session)
    issued: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name in services:
        for holder in await perms.holders(name):
            user = holder.get("user") or {}
            uid = str(user.get("id") or "")
            if not uid or uid in seen:
                continue
            seen.add(uid)
            if await keys.of_subject(UUID(uid)) is not None:
                continue
            info = await perms.user(uid) or {}
            row = await keys.issue(
                UUID(uid),
                subject_type=str(info.get("user_type") or "human"),
                subject_name=str(info.get("name") or user.get("name") or ""),
                actor=actor,
            )
            issued.append((uid, row.subject_name))
    return issued
