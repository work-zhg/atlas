"""把技能包拷进这个人的技能目录。

## 布局：一个人一份，按技能、版本分子目录

    {user}/skills/{slug}/{version}/    ← 这个人所有会话共用；沙箱里是 /data/skills/{slug}/{version}/
    _skills/{slug}/{version}/          ← 源仓库，全局只读，在桶根（沙箱里看不到）

同一版技能只拷一次，新会话、子会话大多不用再拷；不同智能体可能绑同一技能的不同版本，按版本分开才
不会互相覆盖。每个会话的提示词只列出自己智能体绑定的技能和路径（清单来自智能体的配置，不扫描目录），
目录里别的技能不会混进来。

★ 目录可写、一个人共用：会话里改了技能，这个人所有绑同一版本的会话都会看到（用户 2026-11-09 定的）。
  `run.agent_version_id` 回答的是「绑的是哪一版」，不是「这一轮实际读到的内容」。

## 时机：缺了才拷，拷完写完成标记

会话创建时拷（建会话的延迟比首轮对话的等待好接受），每轮装配时再确认一次（老会话、改了智能体的技能
配置都靠这一次补上）。确认只是一次 HEAD：标记在就跳过。没有标记的目录当作没拷完，重拷一遍（同名对象
覆盖，幂等）。

★ 例外的写入路径：拷贝用对象存储的服务端复制，直写 {user}/skills/…，不经沙箱。源仓库不在沙箱里，只能
  这样；目标目录刚确认过没拷完，沙箱里没人用过它，不会和 Pod 里的缓存冲突。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from atlas_engine.contracts import SkillRef

from ...domain.workspace_layout import WorkspaceLayout
from ...errors import CapabilityUnavailable

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "COMPLETE_MARKER",
    "SkillMeta",
    "SkillUnavailable",
    "resolve_skills",
    "seed_session_skills",
    "skill_dir",
    "skill_refs_for",
]


#: 拷完最后写的完成标记（技能版本目录里的隐藏文件）
COMPLETE_MARKER = ".atlas-copied"


class SkillUnavailable(CapabilityUnavailable):
    """引用的技能版本在技能区不存在。

    ★ 冒泡成 run.failed，不静默跳过。少拷几个文件就默默开始的话，
      表现是模型「照技能说的做了但脚本不存在」—— 比明确报错难查得多。
    """


@dataclass(frozen=True)
class SkillMeta:
    """技能的展示元数据。

    来自配置平面的 skill 表（本期尚未接入时由调用方直接构造）。
    ★ 不从 SKILL.md 的 frontmatter 解析 —— 那是发布时的输入，
      不是运行时的数据源。
    """

    slug: str
    version: int
    description: str


async def resolve_skills(
    refs: Sequence[Any], directory: Any, *, with_descriptions: bool = True
) -> tuple[list[SkillMeta], list[dict[str, Any]]]:
    """spec 里钉死的技能引用（SkillRefSpec）→ (要装的元数据, 被跳过的说明)。

    ★ 已紧急下架的版本跳过（设计 §7.3）：下架是为了止损，不是让 agent 不可用，
      所以不让会话 / run 失败；跳过的清单交给调用方变成 skill.skipped 事件。
    ★ description 的权威来源是**技能应用**，不是会话副本里的 frontmatter ——
      技能进了会话就能被 execute 改，而 description 是模型的**路由依据**
      （决定要不要展开正文）。让它始终来自那份经过审查的版本。
    ★ 没接技能应用（directory.configured=False）时退回接入前的行为：照 spec
      投送，描述以 slug 兜底。
    ★ 下架清单拉不到时**放行**（打 warning）：拿不到「哪些被下架了」不该让所有
      带技能的会话一起失败 —— 下架晚生效一会儿是可以接受的代价。

    主 agent（会话创建、每轮装配）与子智能体（委派时建子会话）共用这一个入口 ——
    分叉过一次：主 agent 的技能一度根本没被投送，而子智能体的投了。
    """
    if not refs:
        return [], []
    try:
        revoked = await directory.revoked()
    except CapabilityUnavailable:
        logger.warning("拉不到技能下架清单，本次不做下架过滤", exc_info=True)
        revoked = frozenset()

    metas: list[SkillMeta] = []
    skipped: list[dict[str, Any]] = []
    for ref in refs:
        if (ref.slug, ref.version) in revoked:
            skipped.append({"slug": ref.slug, "version": ref.version, "reason": "revoked"})
            continue
        description = ref.slug
        if with_descriptions and directory.configured:
            try:
                description = (await directory.get(ref.slug, ref.version)).description
            except LookupError as exc:
                msg = f"技能 {ref.slug}@v{ref.version} 在技能目录里不存在或未发布"
                raise SkillUnavailable(msg) from exc
        metas.append(SkillMeta(slug=ref.slug, version=ref.version, description=description))
    return metas, skipped


def skill_dir(slug: str, version: int | str) -> str:
    """技能版本目录在沙箱里的路径（模型看到的）。"""
    return WorkspaceLayout.real(WorkspaceLayout.skill(slug, version))


def skill_refs_for(metas: Sequence[SkillMeta]) -> list[SkillRef]:
    """元数据 → 中间件要的 SkillRef（含模型看到的路径）。"""
    return [
        SkillRef(
            name=m.slug, description=m.description, path=f"{skill_dir(m.slug, m.version)}/SKILL.md"
        )
        for m in metas
    ]


#: 技能源仓库（全局只读，配置平面按版本发布）。会话副本从这里拷
_SKILLS_REPO = "_skills"


def _skill_prefix(fs: Any, slug: str, version: int) -> str:
    """技能源仓库里某个版本的绝对 key 前缀（全局只读）。"""
    return f"{_SKILLS_REPO}/{slug}/{version}/"


async def seed_session_skills(fs: Any, metas: Sequence[SkillMeta]) -> list[SkillRef]:
    """确保技能都在这个人的技能目录里（缺了才拷），返回中间件要的清单。

    幂等：完成标记在就跳过 —— 会话创建、每轮装配、子会话都会调。
    """
    if not metas:
        return []
    for meta in metas:
        await asyncio.to_thread(_copy_one, fs, meta)
    return skill_refs_for(metas)


def _copy_one(fs: Any, meta: SkillMeta) -> None:
    src_prefix = _skill_prefix(fs, meta.slug, meta.version)
    dst_prefix = fs.layout.key(WorkspaceLayout.skill(meta.slug, meta.version)) + "/"
    marker = f"{dst_prefix}{COMPLETE_MARKER}"
    try:
        fs._s3.head_object(Bucket=fs._bucket, Key=marker)
        return  # 已经拷完
    except Exception:
        pass

    keys: list[str] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": fs._bucket, "Prefix": src_prefix, "MaxKeys": 1000}
        if token:
            kwargs["ContinuationToken"] = token
        page = fs._s3.list_objects_v2(**kwargs)
        keys.extend(item["Key"] for item in page.get("Contents", ()))
        token = page.get("NextContinuationToken")
        if not page.get("IsTruncated") or not token:
            break

    if not keys:
        msg = f"技能 {meta.slug}@v{meta.version} 在技能区不存在"
        raise SkillUnavailable(msg)

    for key in keys:
        rel = key.removeprefix(src_prefix)
        fs._s3.copy_object(
            Bucket=fs._bucket,
            CopySource={"Bucket": fs._bucket, "Key": key},
            Key=f"{dst_prefix}{rel}",
        )
    # ★ 最后写标记：崩在中途的，下次看不到标记就重拷
    fs._s3.put_object(Bucket=fs._bucket, Key=marker, Body=b"")
    logger.info("技能 %s@v%d 已拷入技能目录：%d 个对象", meta.slug, meta.version, len(keys))
