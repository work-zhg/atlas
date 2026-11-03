"""界面上的工作区（code-workspace.html · WorkspaceListing）：列文件、打开、预览、上传。

读和预览直连对象存储（写穿式挂载下文件 close 后就在对象存储里，直接读就行，还能用 Range 和 ETag）；
上传和模型的写走同一个选路 —— 沙箱在跑时经沙箱写，只有一个写入方。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...domain.workspace_layout import Area
from .paths import PathEscape
from .workspace import Workspace, is_hidden

__all__ = ["PreviewObject", "WorkspaceListing"]


class WorkspaceListing:
    """给**用户**看的工作区文件清单（会话右侧「文件」面板）。

    ★ 与模型用的 search 分开：那边按层列举、受 SEARCH_MAX_KEYS 约束、要还原成
      模型视角的路径；这里要的是递归全量 + 大小 + 修改时间，且只看工作区。
    ★ 为什么不靠 file.written 事件：acp 子智能体的 CLI 在 Pod 里直接写挂载的
      工作区，平台侧不会产生任何事件 —— 只看事件的话这类产物永远是空的。
    """

    def __init__(self, fs: Workspace) -> None:
        self._fs = fs

    def _workspace_key(self, path: str, action: str) -> str:
        """只认工作区区的文件（不经界面碰技能区 / 系统区），和模型同一道路径关卡。"""
        fp = self._fs.gate.resolve(path if path.startswith("/") else f"/workspace/{path}")
        if fp.area is not Area.WORKSPACE:
            raise PathEscape(f"只能{action}工作区里的文件：{path!r}")
        return self._fs.objects.key(fp)

    def list(self, *, max_keys: int = 2000) -> tuple[list[dict[str, Any]], bool]:
        prefix = self._fs.layout.key(Area.WORKSPACE, "")
        files: list[dict[str, Any]] = []
        for page in _paginate(self._fs._s3, self._fs._bucket, prefix):
            for item in page.get("Contents", ()):
                rel = item["Key"].removeprefix(prefix)
                if not rel or rel.endswith("/") or is_hidden(rel):  # 目录标记对象不是文件
                    continue
                if len(files) >= max_keys:
                    return files, True
                modified = item.get("LastModified")
                files.append(
                    {
                        "path": rel,
                        "size": int(item.get("Size", 0)),
                        "modified_at": modified.isoformat() if modified else None,
                        # 预览据此判断「内容变了没有」（分片上传时不是 MD5，但仍随内容变）
                        "etag": str(item.get("ETag", "")).strip('"') or None,
                    }
                )
        files.sort(key=lambda f: f["path"])
        return files, False

    def open(self, path: str) -> tuple[Any, int]:
        """→ (流式 body, 字节数)。只允许工作区内的文件 —— 不经这里下载技能区 / 系统区。"""
        key = self._workspace_key(path, "下载")
        obj = self._fs._s3.get_object(Bucket=self._fs._bucket, Key=key)
        return obj["Body"], int(obj.get("ContentLength", 0))

    async def put(self, path: str, data: bytes) -> int:
        """用户上传一个文件到工作区（与 open 同一道路径关卡）。→ 字节数。

        ★ 和模型的写走同一个选路：沙箱在跑时经沙箱写，只有一个写入方。原先界面上传一律直连
          对象存储，沙箱在跑时可能和沙箱里的进程互相覆盖。
        """
        if path.endswith("/"):
            raise PathEscape(f"只能上传到工作区里的文件路径：{path!r}")
        self._workspace_key(path, "上传到")
        result = await self._fs.aput(path if path.startswith("/") else f"/workspace/{path}", data)
        if result.error:
            raise OSError(result.error)
        return len(data)

    def fetch(
        self,
        path: str,
        *,
        byte_range: str | None = None,
        if_none_match: str | None = None,
    ) -> PreviewObject:
        """预览站点用的读取：透传 Range 与条件请求，其余与 open 同一道路径关卡。

        ★ 路径校验与 open 完全相同（PathGate + 只认工作区区）—— 会话隔离只有
          这一个关卡，预览不另写一套。
        ★ 文件不存在抛 FileNotFoundError；304 / 416 不是异常，作为状态返回。
        """
        from botocore.exceptions import ClientError

        key = self._workspace_key(path, "预览")
        kwargs: dict[str, Any] = {"Bucket": self._fs._bucket, "Key": key}
        if byte_range:
            kwargs["Range"] = byte_range
        if if_none_match:
            kwargs["IfNoneMatch"] = if_none_match
        try:
            obj = self._fs._s3.get_object(**kwargs)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in ("304", "NotModified"):
                return PreviewObject(status=304, etag=if_none_match)
            if code in ("416", "InvalidRange"):
                return PreviewObject(status=416)
            if code in ("404", "NoSuchKey", "NotFound"):
                raise FileNotFoundError(path) from exc
            raise
        return PreviewObject(
            status=206 if obj.get("ContentRange") else 200,
            body=obj["Body"],
            length=int(obj.get("ContentLength", 0)),
            etag=obj.get("ETag"),
            content_range=obj.get("ContentRange"),
        )


@dataclass(frozen=True)
class PreviewObject:
    """fetch 的结果。status 为 304 / 416 时没有 body。"""

    status: int
    body: Any = None
    length: int = 0
    etag: str | None = None
    content_range: str | None = None


def _paginate(s3: Any, bucket: str, prefix: str) -> Any:
    """遍历一个前缀下的全部对象（无 delimiter，递归）。"""
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
        if token:
            kwargs["ContinuationToken"] = token
        page = s3.list_objects_v2(**kwargs)
        yield page
        token = page.get("NextContinuationToken")
        if not page.get("IsTruncated") or not token:
            return
