"""原始文件的对象存储。

布局：{oss_prefix}/{dataset_uuid}/{document_uuid}/{文件名}

★ 同步接口（boto3 是同步的）；调用方用 asyncio.to_thread 包。
★ 与 atlas-config 各自实现，不抽公共包（同 auth.py 的理由）。
"""

from __future__ import annotations

from typing import Any, Protocol

from .settings import KnowledgeSettings

__all__ = ["FileStorage", "S3FileStorage", "StorageNotConfigured", "make_storage"]

_DELETE_BATCH = 1000


class StorageNotConfigured(RuntimeError):
    """没配对象存储。上传依赖它，不回落本地实现。"""


class FileStorage(Protocol):
    def put(self, key: str, data: bytes, content_type: str | None = None) -> None: ...
    def get(self, key: str) -> bytes: ...
    def delete_prefix(self, prefix: str) -> int: ...


class S3FileStorage:
    def __init__(self, client: Any, bucket: str) -> None:
        self._s3 = client
        self._bucket = bucket

    def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        extra = {"ContentType": content_type} if content_type else {}
        self._s3.put_object(Bucket=self._bucket, Key=key, Body=data, **extra)

    def get(self, key: str) -> bytes:
        return self._s3.get_object(Bucket=self._bucket, Key=key)["Body"].read()  # type: ignore[no-any-return]

    def delete_prefix(self, prefix: str) -> int:
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
                break
        for i in range(0, len(keys), _DELETE_BATCH):
            self._s3.delete_objects(
                Bucket=self._bucket,
                Delete={
                    "Objects": [{"Key": k} for k in keys[i : i + _DELETE_BATCH]],
                    "Quiet": True,
                },
            )
        return len(keys)


def make_storage(settings: KnowledgeSettings) -> S3FileStorage:
    if not settings.storage_configured:
        raise StorageNotConfigured("没有配置对象存储（OSS_BUCKET / ATLAS_KB_OSS_BUCKET）")

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
            # ★ botocore ≥ 1.36 的默认上传校验和不被 OSS 的 S3 兼容层认（同运行时）
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )
    return S3FileStorage(client, settings.oss_bucket or "")
