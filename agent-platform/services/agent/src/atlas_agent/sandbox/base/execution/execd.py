"""SandboxExec 和 FileSession 的 execd 实现（防腐层）。

    ExecdChannel       按 sandbox_uuid 经生命周期的 access 取 execd 的地址和令牌（进程内缓存，
                       连不上就作废重取）；后台提交、起常驻服务、游标读、状态、中断、指标；顺带记活动
    ExecdFileSession   /files/info、/files/download、/files/upload、/files/search、DELETE /files

execd 的协议细节都在这里翻译：
★ 命令只走后台模式：前台命令在连接断开时会被 SIGKILL（上游 runtime/command.go）；后台命令不随连接
  走，完成后输出保留 24 小时，断线后用同一个 id 和游标接着读。
★ 读到 SSE 里的 init 事件才算提交成功；之前断开翻成 SubmitUnknown（命令可能已经开始）。
★ 游标只用服务端给的值（响应头 EXECD-COMMANDS-TAIL-CURSOR）：规范说是行号，实现是字节偏移。
★ 不认识的命令有两种回法（状态 404、日志 400），都翻成 CommandGone。
★ 没有「列出命令」的接口：提交时把 id 记进账本，查活动时逐个查 status 确认。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

import httpx

from ..lifecycle.runtime import SandboxAccess
from ..lifecycle.server import LifecycleError
from .ledger import ActivityLedger
from .ports import AccessProvider, FileSession
from .values import (
    CommandGone,
    CommandSpec,
    CommandStatus,
    ExecdRejected,
    ExecdUnreachable,
    FileEntry,
    LogChunk,
    ResourceUsage,
    SandboxActivity,
    SubmitUnknown,
    UsageScope,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

__all__ = ["TOKEN_HEADER", "ExecdChannel", "ExecdFileSession"]

logger = logging.getLogger(__name__)

TOKEN_HEADER = "X-EXECD-ACCESS-TOKEN"
_TAIL_CURSOR = "EXECD-COMMANDS-TAIL-CURSOR"


class ExecdChannel:
    """SandboxExec 的实现。一个进程一个，多个会话共用（缓存按 sandbox_uuid）。"""

    def __init__(
        self,
        runtime: AccessProvider,
        ledger: ActivityLedger,
        *,
        usage_scope: UsageScope = "node",
        timeout_s: float = 30.0,
        files_timeout_s: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
        files_transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._runtime = runtime
        self._ledger = ledger
        self._usage_scope: UsageScope = usage_scope
        self._timeout = timeout_s
        self._files_timeout = files_timeout_s
        self._transport = transport
        self._files_transport = files_transport
        self._targets: dict[UUID, SandboxAccess] = {}

    # ------------------------------------------------------------------ 地址和令牌

    async def _target(self, sandbox_uuid: UUID) -> SandboxAccess:
        """缓存命中就用；没有就经 access 取（沙箱不在抛 SandboxGone）。

        ★ lifecycle server 连不上或回 5xx：翻成 ExecdUnreachable，和连不上 execd 同一类 —— 跟随接着
          重试、提交报不可用。只有它说「不存在」（404，access 已翻成 SandboxGone）才是沙箱没了；
          其余 4xx 是拒绝，翻成 ExecdRejected。
        """
        if (hit := self._targets.get(sandbox_uuid)) is not None:
            return hit
        try:
            target = await self._runtime.access(sandbox_uuid)
        except LifecycleError as exc:
            if exc.status is None or exc.status >= 500:
                raise ExecdUnreachable(f"取沙箱 {sandbox_uuid} 的地址失败：{exc}") from exc
            raise ExecdRejected(f"取沙箱 {sandbox_uuid} 的地址被拒绝：{exc}") from exc
        self._targets[sandbox_uuid] = target
        return target

    def _forget(self, sandbox_uuid: UUID) -> None:
        self._targets.pop(sandbox_uuid, None)

    def _client(self, target: SandboxAccess) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=target.url.rstrip("/"),
            timeout=self._timeout,
            transport=self._transport,
            trust_env=False,
            headers={TOKEN_HEADER: target.token.get_secret_value()},
        )

    async def _request(
        self, sandbox_uuid: UUID, method: str, path: str, **kw: Any
    ) -> httpx.Response:
        target = await self._target(sandbox_uuid)
        try:
            async with self._client(target) as client:
                return await client.request(method, path, **kw)
        except httpx.TransportError as exc:
            self._forget(sandbox_uuid)  # 下一次重新解析：沙箱可能换了地址，也可能已经没了
            raise ExecdUnreachable(f"连不上沙箱 {sandbox_uuid} 的 execd：{exc}") from exc

    # ------------------------------------------------------------------ 命令

    async def ping(self, sandbox_uuid: UUID) -> bool:
        """execd 回话（任何非 5xx）就算就绪。沙箱刚建好时 execd 可能还没起来。"""
        try:
            return (await self._request(sandbox_uuid, "GET", "/ping")).status_code < 500
        except ExecdUnreachable:
            return False

    async def start(self, sandbox_uuid: UUID, cmd: CommandSpec) -> str:
        """后台提交，读到 init 事件就返回命令 id；此后命令和这条连接无关。记进活动账本。"""
        cmd_id = await self.spawn(sandbox_uuid, cmd)
        await self._ledger.started(sandbox_uuid, cmd_id)
        return cmd_id

    async def spawn(self, sandbox_uuid: UUID, cmd: CommandSpec) -> str:
        """起一个常驻服务（如会话的 bridge）：同样后台提交，但不记活动 —— 它一直在跑，记了的话
        沙箱永远不闲置；它的活动由它服务的业务自己判断（CLI 看 run 的状态）。"""
        target = await self._target(sandbox_uuid)
        body: dict[str, Any] = {"command": cmd.command, "cwd": cmd.cwd, "background": True}
        if cmd.timeout_s:
            body["timeout"] = int(cmd.timeout_s) * 1000  # 毫秒；省略 = 不限
        if cmd.env:
            body["envs"] = dict(cmd.env)
        try:
            return await self._submit(target, body)
        except httpx.TransportError as exc:
            self._forget(sandbox_uuid)
            # ★ 请求可能已经到了 execd：不重试，交给上层告诉模型先检查现场
            raise SubmitUnknown(f"提交途中和沙箱 {sandbox_uuid} 断开：命令可能已经开始") from exc

    async def _submit(self, target: SandboxAccess, body: dict[str, Any]) -> str:
        async with (
            self._client(target) as client,
            client.stream("POST", "/command", json=body) as resp,
        ):
            if resp.status_code >= 400:
                text = (await resp.aread()).decode(errors="replace")[:300]
                raise ExecdRejected(f"execd 返回 {resp.status_code}：{text}")
            async for event in _events(resp.aiter_lines()):
                if event.get("type") == "init":
                    return str(event["text"])
                if event.get("type") == "error":
                    raise ExecdRejected(f"execd 拒绝执行：{event}")
        raise SubmitUnknown("没收到 init 事件：命令可能已经开始，结果未知")

    async def read(self, sandbox_uuid: UUID, cmd_id: str, cursor: int) -> LogChunk:
        """游标之后的输出和下一个游标。★ 命令在跑时本来就算忙，读输出不记活动。"""
        resp = await self._request(
            sandbox_uuid, "GET", f"/command/{cmd_id}/logs", params={"cursor": cursor}
        )
        if resp.status_code in (400, 404):
            await self._ledger.finished(sandbox_uuid, cmd_id)
            raise CommandGone(cmd_id)
        _raise_for(resp)
        return LogChunk(
            resp.content, int(resp.headers.get(_TAIL_CURSOR, cursor + len(resp.content)))
        )

    async def status(self, sandbox_uuid: UUID, cmd_id: str) -> CommandStatus:
        """是否在跑、退出码、启动错误。查到已结束或 CommandGone 时从账本划掉并记一次活动。"""
        try:
            status = await self._status(sandbox_uuid, cmd_id)
        except CommandGone:
            await self._ledger.finished(sandbox_uuid, cmd_id)
            raise
        if not status.running:
            await self._ledger.finished(sandbox_uuid, cmd_id)
        return status

    async def confirm_running(self, sandbox_uuid: UUID, cmd_id: str) -> None:
        """跟随方确认命令还在跑：在活动账本里再登记一次（幂等），Redis 丢过数据也能补回来。

        ★ 不放进 status：常驻服务（会话的 bridge）也查 status，它不能被记成在跑的命令，否则沙箱
          永远不闲置。
        """
        await self._ledger.still_running(sandbox_uuid, cmd_id)

    async def _status(self, sandbox_uuid: UUID, cmd_id: str) -> CommandStatus:
        resp = await self._request(sandbox_uuid, "GET", f"/command/status/{cmd_id}")
        if resp.status_code == 404:
            raise CommandGone(cmd_id)
        _raise_for(resp)
        body = resp.json() or {}
        code = body.get("exit_code")
        return CommandStatus(
            running=bool(body.get("running")),
            exit_code=int(code) if code is not None else None,
            error=str(body.get("error") or "") or None,
        )

    async def interrupt(self, sandbox_uuid: UUID, cmd_id: str) -> None:
        """execd 先 SIGTERM 整个进程组，宽限后 SIGKILL。"""
        await self._request(sandbox_uuid, "DELETE", "/command", params={"id": cmd_id})

    async def usage(self, sandbox_uuid: UUID) -> ResourceUsage | None:
        """CPU 使用率和统计口径；取不到返回 None。"""
        try:
            resp = await self._request(sandbox_uuid, "GET", "/metrics")
            resp.raise_for_status()
            return ResourceUsage(float(resp.json()["cpu_used_pct"]), self._usage_scope)
        except (ExecdUnreachable, httpx.HTTPError, KeyError, TypeError, ValueError):
            return None

    # ------------------------------------------------------------------ 活动

    async def activity(self, sandbox_uuid: UUID) -> SandboxActivity:
        """账本里的命令逐个向 execd 确认：跑完的、不认识的划掉（不算活动），问不到就当它还在跑。

        ★ 运行不设上限：宁可晚回收，也不能把正在跑的命令连同沙箱一起删掉。
        """
        alive: set[str] = set()
        for cmd_id in await self._ledger.running(sandbox_uuid):
            try:
                running = (await self._status(sandbox_uuid, cmd_id)).running
            except CommandGone:
                running = False
            except Exception:
                alive.add(cmd_id)
                continue
            if running:
                alive.add(cmd_id)
            else:
                await self._ledger.drop(sandbox_uuid, cmd_id)
        return SandboxActivity(frozenset(alive), await self._ledger.last_active(sandbox_uuid))

    # ------------------------------------------------------------------ 文件

    async def open_files(self, sandbox_uuid: UUID) -> FileSession:
        """异步（要先取地址和令牌），返回的会话是同步的。打开文件算一次活动。"""
        target = await self._target(sandbox_uuid)
        await self._ledger.touch(sandbox_uuid)
        return ExecdFileSession(
            target.url,
            target.token.get_secret_value(),
            timeout_s=self._files_timeout,
            transport=self._files_transport,
        )


class ExecdFileSession:
    """FileSession 的 execd 实现。路径是沙箱里的绝对路径（/workspace/…）。404 翻成不存在。"""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout_s: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=url.rstrip("/"),
            timeout=timeout_s,
            transport=transport,
            trust_env=False,
            headers={TOKEN_HEADER: token},
        )

    def close(self) -> None:
        self._client.close()

    def _info(self, path: str) -> dict[str, Any] | None:
        resp = self._client.get("/files/info", params={"path": path})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        info = (resp.json() or {}).get(path)
        return info if isinstance(info, dict) else None

    def size(self, path: str) -> int | None:
        """文件大小；不存在（或不是文件）返回 None。"""
        info = self._info(path)
        if not info or info.get("type", "file") != "file":
            return None
        return int(info.get("size", 0))

    def download(self, path: str) -> bytes | None:
        resp = self._client.get("/files/download", params={"path": path})
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.content

    def upload(self, path: str, data: bytes) -> None:
        """返回时文件已经 close：写穿式挂载下已经落盘。"""
        metadata = json.dumps({"path": path, "mode": 644})
        files: list[tuple[str, Any]] = [
            ("metadata", ("metadata", metadata, "application/json")),
            ("file", (path.rsplit("/", 1)[-1] or "file", data, "application/octet-stream")),
        ]
        self._client.post("/files/upload", files=files).raise_for_status()

    def search(self, directory: str) -> list[FileEntry]:
        """目录下（递归）的全部文件。目录不存在返回空。"""
        resp = self._client.get("/files/search", params={"path": directory, "pattern": "**"})
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        return [
            FileEntry(str(f["path"]), int(f.get("size", 0)))
            for f in resp.json() or []
            if f.get("type", "file") == "file"
        ]

    def remove(self, paths: list[str]) -> None:
        if paths:
            self._client.delete("/files", params=[("path", p) for p in paths]).raise_for_status()

    def mkdir(self, path: str) -> None:
        """建目录（连同不存在的上级），已经有了也算成功。mode 0 = 不改权限。"""
        self._client.post("/directories", json={path: {"mode": 0}}).raise_for_status()


def _raise_for(resp: httpx.Response) -> None:
    if resp.status_code >= 400:
        raise ExecdRejected(f"execd 返回 {resp.status_code}：{resp.text[:300]}")


async def _events(lines: AsyncIterator[str]) -> AsyncIterator[dict[str, Any]]:
    """execd 的 SSE 事件。兼容带 `data:` 前缀与不带前缀（逐行 JSON）两种写法。"""
    async for raw in lines:
        line = raw.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event
