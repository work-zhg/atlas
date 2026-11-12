"""删掉旧布局的会话文件（迁移 0044 之后跑一次：python -m atlas_agent purge-legacy-workspaces）。

旧布局：{user}/{根会话 id}/workspace/…（更早还有同级的 skills/、system/）。用户的文件改放
{user}/files/ 之后，这些不再有人引用；用户定的是直接删掉，不搬。

★ 只删「第二段是 UUID」的前缀：{user}/files/、skills/、home/ 一概不碰；第一段不是 UUID 的（不是某个
  人的前缀）也不碰 —— 桶里如果还放了别的东西，不会被误删。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

__all__ = ["LegacyPrefix", "purge_legacy_workspaces"]

_DELETE_BATCH = 1000


@dataclass(frozen=True)
class LegacyPrefix:
    prefix: str
    objects: int
    size: int


def purge_legacy_workspaces(s3: Any, bucket: str, *, dry_run: bool) -> list[LegacyPrefix]:
    """找出（dry_run=False 时删掉）全部旧布局的会话目录 → 每个前缀的对象数和大小。"""
    found: list[LegacyPrefix] = []
    for user_prefix in _prefixes(s3, bucket, ""):
        if not _is_uuid(user_prefix.rstrip("/")):
            continue
        for sub in _prefixes(s3, bucket, user_prefix):
            if not _is_uuid(sub[len(user_prefix) :].rstrip("/")):
                continue  # files/、skills/、home/
            keys, size = [], 0
            for item in _objects(s3, bucket, sub):
                keys.append(item["Key"])
                size += int(item.get("Size", 0))
            found.append(LegacyPrefix(sub, len(keys), size))
            if not dry_run:
                for i in range(0, len(keys), _DELETE_BATCH):
                    batch = keys[i : i + _DELETE_BATCH]
                    s3.delete_objects(
                        Bucket=bucket, Delete={"Objects": [{"Key": k} for k in batch]}
                    )
    return found


def _is_uuid(s: str) -> bool:
    try:
        UUID(s)
    except ValueError:
        return False
    return True


def _pages(s3: Any, bucket: str, prefix: str, **extra: Any) -> Iterator[dict[str, Any]]:
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000, **extra}
        if token:
            kwargs["ContinuationToken"] = token
        page = s3.list_objects_v2(**kwargs)
        yield page
        token = page.get("NextContinuationToken")
        if not page.get("IsTruncated") or not token:
            return


def _prefixes(s3: Any, bucket: str, prefix: str) -> Iterator[str]:
    for page in _pages(s3, bucket, prefix, Delimiter="/"):
        for cp in page.get("CommonPrefixes", ()):
            yield cp["Prefix"]


def _objects(s3: Any, bucket: str, prefix: str) -> Iterator[dict[str, Any]]:
    for page in _pages(s3, bucket, prefix):
        yield from page.get("Contents", ())
