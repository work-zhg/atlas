"""StorageLayout：技能包在对象存储里放在哪（第一层目录是租户）。

    {租户}/_skills/{slug}/{version}/…     发布区 —— 只增不改，运行时从这里拷进会话
    {租户}/_skills_drafts/{upload_id}/…   草稿区 —— 待审查的上传，发布时拷走、被拒时删掉
    _market/skills/{slug}/{version}/…     技能市场（不分租户）：租户安装时拷到自己的发布区
    _skills/{slug}/{version}/…            多租户之前发布的（原地不动，按版本记着的 storage_key 用）

★ 发布区的布局是和运行时的契约：Atlas 按版本记着的 storage_key 直接拷包，不经技能服务。
"""

from __future__ import annotations

from atlas_tenancy import TenantNaming

__all__ = [
    "DRAFTS_PREFIX",
    "MARKET_PREFIX",
    "PUBLISHED_PREFIX",
    "draft_prefix",
    "legacy_prefix",
    "market_prefix",
    "published_prefix",
]

PUBLISHED_PREFIX = "_skills"
DRAFTS_PREFIX = "_skills_drafts"
MARKET_PREFIX = "_market/skills"


def published_prefix(slug: str, version: int) -> str:
    """当前租户里一个已发布版本的目录。"""
    return TenantNaming.object_prefix(PUBLISHED_PREFIX, slug, str(version)) + "/"


def draft_prefix(upload_id: str) -> str:
    """当前租户里一个待审查上传的目录。"""
    return TenantNaming.object_prefix(DRAFTS_PREFIX, upload_id) + "/"


def market_prefix(slug: str, version: int) -> str:
    return f"{MARKET_PREFIX}/{slug}/{version}/"


def legacy_prefix(slug: str, version: int) -> str:
    """多租户之前的已发布目录（登记已有技能 adopt 用）。"""
    return f"{PUBLISHED_PREFIX}/{slug}/{version}/"
