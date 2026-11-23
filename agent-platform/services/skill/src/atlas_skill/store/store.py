"""SkillStore：技能仓库对外的全部操作（c4-model/skill/code-store.html）。

    写草稿    put_draft(upload_id, files)                         上传：先写对象，技能版本再插行
    发布      publish(src, slug, version, file_count)             从草稿区（或技能市场）拷到发布区
    市场      put_market(slug, version, files)                    技能市场发布一个版本
    删        delete(prefix)                                      被拒、发布提交之后删草稿
    读        read(prefix, path) · read_tree(prefix) · read_legacy(slug, version)

★ 上层只给上传 id、slug 和版本号，不拼对象键（布局见 layout.py）。
★ 拷贝的兜底（CopyVerification）：目标前缀非空、库里却没有这个版本，就是上次失败的残留，先清掉再
  拷；拷完数对象，和包内文件数不符就删掉目标、整个发布失败 —— 不会发布一个缺文件的版本。
"""

from __future__ import annotations

import asyncio
import logging

from .layout import draft_prefix, legacy_prefix, market_prefix, published_prefix
from .objects import SkillStorage

logger = logging.getLogger(__name__)

__all__ = ["SkillStore"]


def _copy_verified(objects: SkillStorage, src: str, dst: str, expected: int) -> int:
    residue = objects.list_keys(dst)
    if residue:
        logger.warning("清理 %s 下上次失败的残留：%d 个对象", dst, len(residue))
        objects.delete_tree(dst)
    copied = objects.copy_tree(src, dst)
    if copied != expected:
        objects.delete_tree(dst)
        raise RuntimeError(f"拷贝到 {dst} 的对象数 {copied} ≠ 包内文件数 {expected}")
    return copied


class SkillStore:
    def __init__(self, objects: SkillStorage) -> None:
        self.objects = objects

    async def put_draft(self, upload_id: str, files: dict[str, bytes]) -> str:
        prefix = draft_prefix(upload_id)
        await asyncio.to_thread(self.objects.put_tree, prefix, files)
        return prefix

    async def publish(self, src: str, slug: str, version: int, file_count: int) -> str:
        """拷到当前租户的发布区并核对。→ 发布区前缀。"""
        dst = published_prefix(slug, version)
        await asyncio.to_thread(_copy_verified, self.objects, src, dst, file_count)
        return dst

    async def put_market(self, slug: str, version: int, files: dict[str, bytes]) -> str:
        prefix = market_prefix(slug, version)

        def _put() -> None:
            if self.objects.list_keys(prefix):  # 上次发布失败的残留
                self.objects.delete_tree(prefix)
            self.objects.put_tree(prefix, files)

        await asyncio.to_thread(_put)
        return prefix

    async def delete(self, prefix: str) -> None:
        """删一个前缀（草稿）。删失败只留下孤儿对象，不影响已发布的版本，只记日志。"""
        try:
            await asyncio.to_thread(self.objects.delete_tree, prefix)
        except Exception:
            logger.warning("删除 %s 失败（只留下孤儿对象）", prefix, exc_info=True)

    async def read(self, prefix: str, path: str) -> bytes:
        return await asyncio.to_thread(self.objects.get, f"{prefix}{path}")

    async def read_tree(self, prefix: str) -> dict[str, bytes]:
        return await asyncio.to_thread(self.objects.get_tree, prefix)

    async def read_legacy(self, slug: str, version: int) -> tuple[str, dict[str, bytes]]:
        """多租户之前桶根 _skills/ 下的包（登记已有技能）。→ (前缀, 文件)。"""
        prefix = legacy_prefix(slug, version)
        return prefix, await self.read_tree(prefix)
