"""会话文件预览站点：把一个会话的工作区当成只读静态站点提供。

doc/detail/file-preview.html §06 / §07。

★ 这里的内容是模型写的，一律视为**不可信**。隔离靠响应头
  `Content-Security-Policy: sandbox …`：文档运行在不透明 origin，与平台、
  与 API 都不同源。前端页面里的 iframe 另有一层 sandbox 属性，但新标签
  直接打开时没有 iframe —— 那时只剩这个头。所以**每一个**响应（含 404 /
  410 / 304）都必须带它，见 _security_headers。

★ 永远不要在 sandbox 里加 allow-same-origin：它与 allow-scripts 同时出现时，
  sandbox 形同虚设（同源脚本可以把自己的 sandbox 属性删掉）。
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import posixpath
import re

from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse
from starlette.concurrency import iterate_in_threadpool

from ...config import Settings
from ...deps import RedisDep, SettingsDep
from ...providers.filesystem.oss import PathEscape
from ...services.preview import PreviewService

router = APIRouter(prefix="/previews", tags=["previews"])

#: 不含 allow-same-origin（见模块文档）。前端 iframe 的 sandbox 属性与此同一份清单。
SANDBOX = "allow-scripts allow-forms allow-modals allow-popups allow-downloads"

_SINGLE_RANGE = re.compile(r"^bytes=\d*-\d*$")

#: mimetypes 猜不准或猜出危险结果的扩展名
_TYPE_OVERRIDES = {
    # 新标签直接打开 .md 时显示原文；渲染由前端预览页做
    ".md": "text/plain",
    ".markdown": "text/plain",
    # .ts 会被猜成 video/mp2t
    ".ts": "text/plain",
    ".tsx": "text/plain",
    ".jsx": "text/plain",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".py": "text/plain",
    ".sh": "text/plain",
    ".yaml": "text/plain",
    ".yml": "text/plain",
    ".toml": "text/plain",
    ".log": "text/plain",
}
_TEXTUAL = ("text/", "application/json", "application/xml", "image/svg+xml")


def _content_type(path: str) -> str:
    ext = posixpath.splitext(path)[1].lower()
    ctype = _TYPE_OVERRIDES.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"
    if ctype.startswith(_TEXTUAL):
        ctype += "; charset=utf-8"
    return ctype


def _security_headers(settings: Settings, *, path: str = "") -> dict[str, str]:
    ancestors = " ".join(settings.cors_origins) or "'none'"
    if path.lower().endswith(".pdf"):
        # ★ Chrome 的 PDF 查看器在 CSP sandbox 下不工作（§13 #3）。PDF 不执行
        #   页面脚本，去掉 sandbox 的代价可接受；其余头照旧。
        csp = f"frame-ancestors {ancestors}"
    else:
        csp = f"sandbox {SANDBOX}; frame-ancestors {ancestors}"
    return {
        "Content-Security-Policy": csp,
        # 令牌在路径里：不让它经 Referer 流到页面引用的 CDN
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        # ★ sandbox 文档的 origin 是 null，它加载同站的模块脚本、字体、fetch
        #   都算跨域。凭据在路径里而不在 cookie / 头里，* 是安全的。
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Expose-Headers": "Content-Range, Content-Length, ETag",
        # 每次用 ETag 重新校验：文件随 run 在变（§09）
        "Cache-Control": "no-cache",
    }


def _message(status: int, text: str, settings: Settings) -> Response:
    """iframe 里拿不到状态码 —— 错误也要是一页人看得懂的东西。"""
    return Response(
        content=text,
        status_code=status,
        media_type="text/plain; charset=utf-8",
        headers=_security_headers(settings),
    )


@router.get("/{token}/{path:path}", include_in_schema=False)
async def serve_preview(
    token: str,
    path: str,
    request: Request,
    redis: RedisDep,
    settings: SettingsDep,
) -> Response:
    listing = await PreviewService(redis, settings).resolve(token)
    if listing is None:
        return _message(410, "预览已过期，请回到平台重新打开。", settings)

    # 目录 → index.html（站点语义）
    if path == "" or path.endswith("/"):
        path = f"{path}index.html"
    # 与文件列表同一口径：任一段以点开头即隐藏（.skills/ 不可经此读取）
    if any(seg.startswith(".") for seg in path.split("/") if seg):
        return _message(404, f"没有这个文件：{path}", settings)

    byte_range = request.headers.get("range")
    if byte_range and not _SINGLE_RANGE.match(byte_range.strip()):
        byte_range = None  # 多段 Range 不支持，退回整文件
    try:
        obj = await asyncio.to_thread(
            listing.fetch,
            path,
            byte_range=byte_range,
            if_none_match=request.headers.get("if-none-match"),
        )
    except PathEscape:
        return _message(400, f"路径不在工作区内：{path}", settings)
    except FileNotFoundError:
        return _message(404, f"没有这个文件：{path}", settings)

    headers = _security_headers(settings, path=path)
    if obj.etag:
        headers["ETag"] = obj.etag
    if obj.status in (304, 416):
        return Response(status_code=obj.status, headers=headers)

    headers["Content-Length"] = str(obj.length)
    headers["Accept-Ranges"] = "bytes"
    if obj.content_range:
        headers["Content-Range"] = obj.content_range
    return StreamingResponse(
        iterate_in_threadpool(obj.body.iter_chunks(64 * 1024)),
        status_code=obj.status,
        media_type=_content_type(path),
        headers=headers,
    )


class _MaskPreviewTokens(logging.Filter):
    """访问日志里的预览令牌只留前 6 位 —— 完整令牌就是一份读凭据。"""

    _pattern = re.compile(r"(/v1/previews/[A-Za-z0-9_-]{6})[A-Za-z0-9_-]+")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and record.args:
            record.args = tuple(
                self._pattern.sub(r"\1…", a) if isinstance(a, str) else a for a in record.args
            )
        return True


def install_log_mask() -> None:
    logging.getLogger("uvicorn.access").addFilter(_MaskPreviewTokens())
