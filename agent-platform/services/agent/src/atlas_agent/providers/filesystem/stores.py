"""文件的两种底层（c4-model/agent/code-workspace.html · FileStore）。

    ObjectStoreFiles   直连对象存储（S3 API）。三个区都能走；按布局把区内路径换成 key
    SandboxFiles       经沙箱：读写 Pod 里挂载的 /workspace（和沙箱里的命令同一个挂载）。
                       只服务工作区区

两者语义一致（Workspace 不知道这一次走的是哪个）：按区内路径查大小、读、写、删、列。列举按对象存储的
语义：「前缀匹配 + delimiter 分组」，空目录不出现（对象存储里没有空目录）。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Protocol

from ...domain.workspace_layout import WORKSPACE_MOUNT, Area, WorkspaceLayout
from ..opensandbox.execd import ExecdFiles
from .paths import FilePath

__all__ = ["FileStore", "ObjectStoreFiles", "SandboxFiles"]

#: 批量删除单次的 key 上限。S3 的 DeleteObjects 规定 1000，OSS 同。
_DELETE_BATCH = 1000

#: 一页列举：(这一层的文件, 下一层的「目录」)，都是区内相对路径；目录以 / 结尾
Page = tuple[list[str], list[str]]


class FileStore(Protocol):
    def size(self, path: FilePath) -> int | None: ...

    def get(self, path: FilePath) -> bytes: ...

    def put(self, path: FilePath, data: bytes) -> None: ...

    def remove(self, paths: list[FilePath]) -> None: ...

    def pages(self, prefix: FilePath, *, recursive: bool) -> Iterator[Page]: ...


class ObjectStoreFiles:
    def __init__(self, client: Any, bucket: str, layout: WorkspaceLayout) -> None:
        self._s3, self._bucket, self._layout = client, bucket, layout

    def key(self, path: FilePath) -> str:
        return self._layout.key(path.area, path.rel)

    def size(self, path: FilePath) -> int | None:
        try:
            head = self._s3.head_object(Bucket=self._bucket, Key=self.key(path))
        except Exception:
            return None
        return int(head.get("ContentLength", 0))

    def get(self, path: FilePath) -> bytes:
        return self._s3.get_object(Bucket=self._bucket, Key=self.key(path))["Body"].read()

    def put(self, path: FilePath, data: bytes) -> None:
        self._s3.put_object(Bucket=self._bucket, Key=self.key(path), Body=data)

    def remove(self, paths: list[FilePath]) -> None:
        keys = [self.key(p) for p in paths]
        if len(keys) == 1:
            self._s3.delete_object(Bucket=self._bucket, Key=keys[0])
            return
        for i in range(0, len(keys), _DELETE_BATCH):
            batch = keys[i : i + _DELETE_BATCH]
            self._s3.delete_objects(
                Bucket=self._bucket, Delete={"Objects": [{"Key": k} for k in batch]}
            )

    def pages(self, prefix: FilePath, *, recursive: bool) -> Iterator[Page]:
        """不递归时用 delimiter 模拟「一层」—— 对象存储没有真正的目录。"""
        base = self._layout.key(prefix.area, "")
        kwargs: dict[str, Any] = {
            "Bucket": self._bucket,
            "Prefix": self.key(prefix),
            "MaxKeys": 1000,
        }
        if not recursive:
            kwargs["Delimiter"] = "/"
        token: str | None = None
        while True:
            if token:
                kwargs["ContinuationToken"] = token
            page = self._s3.list_objects_v2(**kwargs)
            yield (
                [item["Key"].removeprefix(base) for item in page.get("Contents", ())],
                [cp["Prefix"].removeprefix(base) for cp in page.get("CommonPrefixes", ())],
            )
            token = page.get("NextContinuationToken")
            if not page.get("IsTruncated") or not token:
                return


class SandboxFiles:
    """经 execd 的文件 API 读写 Pod 里的 /workspace。只服务工作区区（调用方保证）。

    ★ 列举语义与对象存储对齐：execd 列出的是真实目录树，这里换算成「前缀匹配 + delimiter 分组」，
      空目录因此不出现。
    """

    def __init__(self, files: ExecdFiles) -> None:
        self._f = files

    @staticmethod
    def _abs(path: FilePath) -> str:
        assert path.area is Area.WORKSPACE, "只有工作区区会走沙箱"
        return f"{WORKSPACE_MOUNT}/{path.rel}"

    def size(self, path: FilePath) -> int | None:
        return self._f.size(self._abs(path))

    def get(self, path: FilePath) -> bytes:
        data = self._f.download(self._abs(path))
        if data is None:
            raise FileNotFoundError(path.rel)
        return data

    def put(self, path: FilePath, data: bytes) -> None:
        self._f.upload(self._abs(path), data)  # 返回时已 close：写穿式挂载下已经落盘

    def remove(self, paths: list[FilePath]) -> None:
        self._f.remove([self._abs(p) for p in paths])

    def pages(self, prefix: FilePath, *, recursive: bool) -> Iterator[Page]:
        directory = (
            prefix.rel.rsplit("/", 1)[0] if "/" in prefix.rel else ""
        )  # 可以停在半个文件名上
        root = f"{WORKSPACE_MOUNT}/"
        rels = sorted(
            rel
            for f in self._f.search(f"{WORKSPACE_MOUNT}/{directory}".rstrip("/") or WORKSPACE_MOUNT)
            if (rel := str(f["path"]).removeprefix(root)).startswith(prefix.rel)
        )
        if recursive:
            yield rels, []
            return
        direct, groups = [], set()
        for rel in rels:
            rest = rel[len(prefix.rel) :]
            if "/" in rest:
                groups.add(prefix.rel + rest.split("/", 1)[0] + "/")
            else:
                direct.append(rel)
        yield direct, sorted(groups)

    def close(self) -> None:
        self._f.close()
