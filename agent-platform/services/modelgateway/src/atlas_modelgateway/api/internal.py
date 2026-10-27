"""给 Atlas、知识库的内部接口（模型管理方案设计 §5.1、§6.4、§6.7）。

网关（nginx）一律 404，内网免令牌。

    GET  /internal/catalog                  模型目录 + 平台用途（Atlas 缓存后读）
    GET  /internal/subjects/{uid}/access    主体此刻能用哪些对话模型 + 主体的 Key（没有就生成）

★ 「能用哪些模型」和数据面判权用同一份缓存：两道闸门的判断依据一致（§6.4）。
★ Key 自动生成（§6.5）：第一次被取用时生成；主体不在用户中心或已停用时不生成。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from ..errors import Invalid, Unavailable
from .deps import KeysDep, RegistryDep, SubjectsDep
from .schemas import CatalogOut, SubjectAccessOut
from .views import catalog_model_out, purpose_out

router = APIRouter(prefix="/internal", tags=["internal"])


@router.get("/catalog", response_model=CatalogOut)
async def catalog(registry: RegistryDep) -> CatalogOut:
    models = []
    for row in await registry.list():
        models.append(catalog_model_out(row, await registry.current_price(row)))
    purposes = [purpose_out(p) for p in (await registry.purposes()).values()]
    return CatalogOut(models=models, purposes=purposes)


@router.get("/subjects/{uid}/access", response_model=SubjectAccessOut)
async def subject_access(
    uid: str, registry: RegistryDep, keys: KeysDep, subjects: SubjectsDep
) -> SubjectAccessOut:
    try:
        subject = UUID(uid)
    except ValueError as exc:
        raise Invalid("主体必须是用户中心的用户 id") from exc
    allowed = await subjects.usable(str(subject))
    models = sorted(
        m.name
        for m in await registry.list(kind="chat")
        if m.status in ("available", "deprecated") and not m.disabled and allowed.allows(m.name)
    )
    row = await keys.of_subject(subject)
    if row is None:
        try:
            info = await subjects.info(str(subject))
        except Unavailable:
            info = None  # 用户中心不可用：这次不生成，下次再说
        if info is not None and info.active:
            row = await keys.ensure(info)
    return SubjectAccessOut(
        subject=str(subject),
        models=models,
        all_models=allowed.all_models,
        key=row.api_key if row is not None and row.status == "active" else None,
        key_status=row.status if row is not None else "none",  # type: ignore[arg-type]
    )
