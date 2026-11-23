"""对象存储（防腐层）：S3 兼容（MinIO、OSS）的读写、按前缀列、拷、删。

★ 同步接口：boto3 是同步的。调用方（技能仓库 store.py）用 asyncio.to_thread 包。
★ 布局（哪个前缀放什么）不在这里：见 layout.py。
"""

from __future__ import annotations

from typing import Any, Protocol

from ..settings import SkillSettings

__all__ = ["S3SkillStorage", "SkillStorage", "StorageNotConfigured", "make_storage"]

_DELETE_BATCH = 1000


class StorageNotConfigured(RuntimeError):
    """没配对象存储。技能的发布与查看都依赖它，不回落任何本地实现。"""


class SkillStorage(Protocol):
    def put_tree(self, prefix: str, files: dict[str, bytes]) -> None: ...
    def get_tree(self, prefix: str) -> dict[str, bytes]: ...
    def get(self, key: str) -> bytes: ...
    def list_keys(self, prefix: str) -> list[str]: ...
    def copy_tree(self, src_prefix: str, dst_prefix: str) -> int: ...
    def delete_tree(self, prefix: str) -> int: ...


class S3SkillStorage:
    def __init__(self, client: Any, bucket: str) -> None:
        self._s3 = client
        self._bucket = bucket

    def put_tree(self, prefix: str, files: dict[str, bytes]) -> None:
        for path, data in files.items():
            self._s3.put_object(Bucket=self._bucket, Key=f"{prefix}{path}", Body=data)

    def get(self, key: str) -> bytes:
        return self._s3.get_object(Bucket=self._bucket, Key=key)["Body"].read()  # type: ignore[no-any-return]

    def get_tree(self, prefix: str) -> dict[str, bytes]:
        return {key.removeprefix(prefix): self.get(key) for key in self.list_keys(prefix)}

    def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self._bucket, "Prefix": prefix, "MaxKeys": 1000}
            if token:
                kwargs["ContinuationToken"] = token
            page = self._s3.list_objects_v2(**kwargs)
            keys.extend(item["Key"] for item in page.get("Contents", ()))
            token = page.get("NextContinuationToken")
            if not page.get("IsTruncated") or not token:
                return keys

    def copy_tree(self, src_prefix: str, dst_prefix: str) -> int:
        keys = self.list_keys(src_prefix)
        for key in keys:
            self._s3.copy_object(
                Bucket=self._bucket,
                CopySource={"Bucket": self._bucket, "Key": key},
                Key=f"{dst_prefix}{key.removeprefix(src_prefix)}",
            )
        return len(keys)

    def delete_tree(self, prefix: str) -> int:
        keys = self.list_keys(prefix)
        for i in range(0, len(keys), _DELETE_BATCH):
            batch = keys[i : i + _DELETE_BATCH]
            self._s3.delete_objects(
                Bucket=self._bucket,
                Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
            )
        return len(keys)


def make_storage(settings: SkillSettings) -> S3SkillStorage:
    if not settings.storage_configured:
        raise StorageNotConfigured("没有配置对象存储（OSS_BUCKET / ATLAS_SKILL_OSS_BUCKET）")

    import boto3
    from botocore.config import Config

    client = boto3.client(
        "s3",
        endpoint_url=settings.oss_endpoint,
        region_name=settings.oss_region,
        aws_access_key_id=(
            settings.oss_access_key_id.get_secret_value() if settings.oss_access_key_id else None
        ),
        aws_secret_access_key=(
            settings.oss_access_key_secret.get_secret_value()
            if settings.oss_access_key_secret
            else None
        ),
        config=Config(
            s3={"addressing_style": settings.oss_addressing_style},
            # ★ 与运行时同一个坑：botocore ≥ 1.36 的默认上传校验和不被 OSS / GCS 的
            #   S3 兼容层认，报的却是 SignatureDoesNotMatch（见 server oss 工厂）。
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )
    return S3SkillStorage(client, settings.oss_bucket or "")
