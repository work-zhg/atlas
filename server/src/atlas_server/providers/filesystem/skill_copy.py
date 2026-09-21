"""把技能包拷进会话的 skills 前缀。

## 布局：与 workspace 平级

    {user}/{thread}/workspace/    ← 可被子智能体共享挂载
    {user}/{thread}/skills/       ← 恒为自己的
    _skills/{slug}/{version}/     ← 源仓库，全局只读

模型看到的是 `/skills/<slug>/`，由 `OssFilesystem._to_key` 路由过去。
Pod 侧是一个**独立的挂载点** —— 所以 `execute` 能直接跑技能里的脚本，
不需要第二条投送路径、不需要物化的幂等判断、不需要清理残留。

代价是每个会话持有一份副本：会话可以改自己的技能，改动只对本会话生效。
`run.agent_version_id` 因此回答的是「这个会话开始时拷了哪一版」，而不是
「这一轮实际用的是什么」—— 要还原实际内容，会话副本本身就在 OSS 里。

## 时机

**会话创建**，不是首次 run。用户对「新建会话」的延迟容忍度更高，而首轮
对话的等待是最刺眼的。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from atlas_engine.contracts import SkillRef

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "SKILLS_MOUNT",
    "SkillMeta",
    "SkillUnavailable",
    "seed_session_skills",
    "skill_metas_for",
    "skill_refs_for",
]

#: 模型看到的技能挂载点。★ 它**不在 workspace 之内** —— OssFilesystem 把
#: 这个前缀路由到本会话的 skills 前缀（与 workspace 平级的兄弟目录）。
#:
#: 为什么必须平级：workspace 是**共享挂载**的 —— 子智能体的 Pod 挂父 agent
#: 的 workspace。技能若住在里面，挂载时会连父的技能一起带上，而它应该有
#: 自己的一套。平级之后这是结构性的，不是约定：挂载根本带不上。
SKILLS_MOUNT = "/skills"

#: 并发拷贝的批大小。CopyObject 是服务端拷贝（不下载不上传），
#: 单对象 10–50ms；ui-ux-pro-max 有 76 个对象，并发 20 时 < 1s。
_COPY_CONCURRENCY = 20


class SkillUnavailable(RuntimeError):
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


def skill_metas_for(refs: Sequence[Any]) -> list[SkillMeta]:
    """spec 里钉死的技能引用（SkillRefSpec）→ 展示元数据。

    ★ description 的权威来源是**配置平面的 skill 表**，不是会话副本里的
      frontmatter —— 技能进了会话就能被 execute 改，而 description 是模型的
      **路由依据**（决定要不要展开正文）。让它始终来自那份经过审核的版本，
      正文则可以按会话适配：改不动别人对你的第一印象，但可以改自己的做法。
    ★ 配置平面尚未接入，暂用 slug 兜底；接入后从缓存读。

    主 agent（会话创建）与子智能体（委派时建子会话）共用这一个入口 ——
    分叉过一次：主 agent 的技能一度根本没被投送，而子智能体的投了。
    """
    return [SkillMeta(slug=r.slug, version=r.version, description=r.slug) for r in refs]


def skill_refs_for(metas: Sequence[SkillMeta]) -> list[SkillRef]:
    """元数据 → 中间件要的 SkillRef（含模型看到的路径）。"""
    return [
        SkillRef(
            name=m.slug,
            description=m.description,
            path=f"{SKILLS_MOUNT}/{m.slug}/SKILL.md",
        )
        for m in metas
    ]


def _skill_prefix(fs: Any, slug: str, version: int) -> str:
    """技能源仓库里某个版本的绝对 key 前缀（全局只读）。"""
    return f"{fs._skills_repo}/{slug}/{version}/"


async def seed_session_skills(fs: Any, metas: Sequence[SkillMeta]) -> list[SkillRef]:
    """把技能包拷进本会话的 skills 前缀，返回中间件要的清单。

    幂等：目标已存在同名对象则跳过 —— 会话重建、创建重试都会撞到。
    """
    if not metas:
        return []
    for meta in metas:
        await asyncio.to_thread(_copy_one, fs, meta)
    return skill_refs_for(metas)


def _copy_one(fs: Any, meta: SkillMeta) -> None:
    src_prefix = _skill_prefix(fs, meta.slug, meta.version)
    # ★ 拷进**本会话的** skills 前缀，不是 workspace —— 子智能体因此
    #   有自己的一套，而它挂的 workspace 是父 agent 的。
    dst_prefix = f"{fs._skills}/{meta.slug}/"

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
    logger.info("技能 %s@v%d 已拷入会话：%d 个对象", meta.slug, meta.version, len(keys))
