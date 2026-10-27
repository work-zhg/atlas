"""存量迁移（模型管理方案设计 §6.3「存量迁移」）：上线时先保持现状。

    import_catalog   把 Atlas 导出的模型目录登记进网关
                     （Atlas：python -m atlas_agent export-models）。
                     能力用网关的词汇（Atlas 导出时按它原来的能力表和代码里的规则换算好）；
                     原来标为可用的直接上线
    grant_all_users  给根部门（含下级）授某个模型的「使用」：保持迁移前「谁都能用」
    grant_digital    以 Agent 为权限的 Agent：它的数字员工按 Agent 现有配置里用到的模型直接授权
    set_purposes     平台用途的默认模型：沿用 Atlas 原来写在部署配置里的模型

★ 可以重复执行：登记过的模型跳过，授权由用户中心去重。
★ 操作人记为执行迁移的模型管理员（记入审计）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import outbound_headers
from .errors import GatewayError
from .perms import Grant, Permissions
from .registry import Registry
from .settings import GatewaySettings

logger = logging.getLogger(__name__)

__all__ = ["ImportReport", "grant_all_users", "import_catalog", "root_dept"]

_ATTRS = (
    "display_name",
    "description",
    "provider",
    "tags",
    "upstream_model",
    "protocols",
    "input_modalities",
    "capabilities",
    "context_window",
    "max_output_tokens",
    "dimensions",
    "max_batch",
    "data_policy",
)


@dataclass
class ImportReport:
    imported: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    activated: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    granted: list[str] = field(default_factory=list)
    purposes: list[str] = field(default_factory=list)


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


async def grant_all_users(perms: Permissions, model: str, *, root: str, operator: str) -> None:
    await perms.grant(model, operator, [Grant("READ", "dept", root, include_sub=True)])


async def import_catalog(
    session: AsyncSession,
    settings: GatewaySettings,
    perms: Permissions,
    exported: dict[str, Any],
    *,
    owner: str,
    root: str | None,
) -> ImportReport:
    """exported：Atlas 导出的 {models, digital_employees, purposes}。root：给了就授全员「使用」。"""
    report = ImportReport()
    registry = Registry(session, settings)
    for item in exported.get("models") or []:
        name = str(item["name"])
        if await registry.find(name) is not None:
            report.existing.append(name)
            continue
        attrs = {k: item[k] for k in _ATTRS if item.get(k) is not None}
        try:
            async with session.begin_nested():
                await registry.create(
                    perms, name=name, kind=str(item.get("kind") or "chat"), actor=owner, **attrs
                )
        except GatewayError as exc:
            report.skipped.append((name, exc.message))
            continue
        report.imported.append(name)
        if item.get("available"):
            try:
                async with session.begin_nested():
                    await registry.activate(name, actor=owner)
                report.activated.append(name)
            except GatewayError as exc:
                report.skipped.append((name, f"已登记，但没能上线：{exc.message}"))
    known = {m.name: m for m in await registry.list()}
    if root is not None:
        for name, row in known.items():
            if row.kind == "chat" and name in report.imported:
                await grant_all_users(perms, name, root=root, operator=owner)
                report.granted.append(f"{name} → 全员")
    for de in exported.get("digital_employees") or []:
        uid = str(de["uid"])
        models = [m for m in de.get("models") or [] if m in known and known[m].kind == "chat"]
        for name in models:
            await perms.grant(name, owner, [Grant("READ", "user", uid)])
            report.granted.append(f"{name} → 数字员工 {de.get('name') or uid}")
    for code, model in (exported.get("purposes") or {}).items():
        if not model or model not in known:
            continue
        current = (await registry.purposes()).get(code)
        if current is None or current.default_model:
            continue  # 不认识的用途，或已经配过：不覆盖
        try:
            async with session.begin_nested():
                await registry.set_purpose(
                    code, default_model=model, allowed_models=[], actor=owner
                )
        except GatewayError as exc:
            report.skipped.append((f"用途 {code}", exc.message))
            continue
        report.purposes.append(f"{code} = {model}")
    return report
