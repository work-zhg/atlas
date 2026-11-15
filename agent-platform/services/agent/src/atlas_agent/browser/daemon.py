"""浏览器 · 宿主：给沙箱里的浏览器守护发平台命令（c4-model/agent/browser/code-browser-host.html）。

    DaemonClient   都经 execd：起守护（常驻，不记活动）、执行一条平台命令等它结束、传文件
    ViewKey        画面口令 = HMAC(部署密钥, sandbox_uuid)：不存，转发画面时任何实例都能再算出来
    Captured       接管窗口关掉时的结果：接管前后 cookie 变了的域名，和浏览器此刻的登录态

守护的命令行（docker/sandbox-browser/atlas_browserd，code-browser-daemon.html）：

    atlas-browserd serve                                常驻：起虚拟屏幕、Chromium、画面服务并看着
    atlas-browserd status --wait N                      等 CDP 就绪
    atlas-browserd state import FILE                    导入登录态（导完删文件）
    atlas-browserd state export [--sites a,b] --out F   导出这些站点的登录态
    atlas-browserd state forget SITE                    清掉一个站点的 cookie 和本地存储
    atlas-browserd takeover open ID URL                 记下此刻的 cookie，开接管窗口
    atlas-browserd takeover close ID --out F            关窗口，比较前后，导出
    atlas-browserd clipboard set F / get --out F        写入 / 读出虚拟屏幕的剪贴板

  一次性的命令把结果写成 stdout 最后一行 JSON；失败时退出码非 0，JSON 里有 error。

★ 不另开控制端口：execd 已经有令牌、执行账本和活动记录，多开一个端口就要多一套鉴权。
★ 登录态文件只放在沙箱本地盘（/tmp/atlas-browser），传完、取完就删，不经过 /data。
★ 只拿 sandbox_uuid：沙箱的地址和令牌在沙箱组件里。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import shlex
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from pydantic import SecretStr

from ..sandbox.base.execution.ports import SandboxExec
from ..sandbox.base.execution.values import CommandGone, CommandSpec
from ..sandbox.provisioning.model import SandboxRef
from .login import BrowserNotConfigured, Site, StateBundle

__all__ = ["DAEMON", "STATE_DIR", "Captured", "DaemonClient", "DaemonError", "ViewKey"]

logger = logging.getLogger(__name__)

#: 守护的命令名（浏览器镜像里装在 PATH 上）
DAEMON = "atlas-browserd"
#: 沙箱本地盘上守护的目录：profile、登录态文件、日志（/tmp 是 emptyDir，不是 /data）
STATE_DIR = "/tmp/atlas-browser"
#: 画面口令进守护的环境变量（守护读完就从环境里删掉）
VIEW_KEY_ENV = "ATLAS_BROWSER_VIEW_KEY"
#: 画面服务 BasicHTTPAuth 的用户名（口令是 ViewKey）
VIEW_USER = "atlas"


class DaemonError(RuntimeError):
    """守护的平台命令失败了（退出码非 0、没有结果、超时）。消息给人看。"""


@dataclass(frozen=True)
class Captured:
    """接管窗口关掉时的结果。

    domains   接管前后新增或值变了的 cookie 所在的域名（只删不增的不算）
    state     浏览器此刻的登录态（全部站点；按 sites() 过滤后再存）
    """

    domains: tuple[str, ...]
    state: StateBundle

    def sites(self) -> set[Site]:
        """这次登录的站点：变了的 cookie 所在的可注册域名。

        ★ 可注册域名只在 Atlas 算（公共后缀表一处）：守护只报域名。
        """
        out: set[Site] = set()
        for domain in self.domains:
            try:
                out.add(Site.of(domain))
            except ValueError:
                continue
        return out


class ViewKey:
    """画面口令：HMAC(部署密钥, sandbox_uuid)。沙箱重建，口令跟着换。

    ★ 模型在沙箱里能读到它也无妨：浏览器本来就归模型操作；它只让集群里能连到这个沙箱的人看画面。
    """

    def __init__(self, secret: SecretStr) -> None:
        self._secret = secret.get_secret_value().encode()

    def secret_for(self, sandbox_uuid: UUID) -> str:
        if not self._secret:
            raise BrowserNotConfigured("没有配置画面密钥（BROWSER_VIEW_SECRET）")
        digest = hmac.new(self._secret, str(sandbox_uuid).encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode().rstrip("=")

    def auth_header(self, sandbox_uuid: UUID) -> dict[str, str]:
        """转发画面时带给沙箱画面服务的请求头（websockify 的 BasicHTTPAuth）。"""
        token = base64.b64encode(f"{VIEW_USER}:{self.secret_for(sandbox_uuid)}".encode()).decode()
        return {"Authorization": f"Basic {token}"}


class DaemonClient:
    """一个进程一个，按 sandbox_uuid 发命令。"""

    #: 一次性平台命令最多等多久（导出、开关窗口都是秒级）
    CALL_TIMEOUT_S = 60
    #: 守护拉起后等 CDP 就绪最多多久（Chromium 冷启动）
    READY_TIMEOUT_S = 90

    def __init__(self, exec: SandboxExec, *, poll_s: float = 0.2) -> None:
        self._exec = exec
        self._poll_s = poll_s

    async def start(
        self,
        ref: SandboxRef,
        view_key: str,
        bundle: StateBundle,
        *,
        env: Mapping[str, str] | None = None,
    ) -> None:
        """拉起守护、等 CDP 就绪，再导入登录态。可重入：守护已经在跑时 serve 直接退出。"""
        sid = ref.sandbox_uuid
        log = f"{STATE_DIR}/daemon.log"
        command = f"mkdir -p {STATE_DIR} && exec {DAEMON} serve >> {log} 2>&1"
        await self._exec.spawn(
            sid, CommandSpec(command, cwd="/tmp", env={**(env or {}), VIEW_KEY_ENV: view_key})
        )
        await self.call(
            ref,
            ["status", "--wait", str(self.READY_TIMEOUT_S)],
            timeout_s=self.READY_TIMEOUT_S + 10,
        )
        if not bundle.empty():
            path = f"{STATE_DIR}/import-{uuid4().hex}.json"
            await self._upload(ref, path, bundle.dumps())
            await self.call(ref, ["state", "import", path])

    async def export(
        self, ref: SandboxRef, sites: Iterable[Site], *, origins: Iterable[str] = ()
    ) -> StateBundle:
        """导出这些站点的登录态：cookie，和这些站点的源的本地存储。

        origins：存过本地存储的源。★ 没开着页面的源守护不知道，由 Atlas 告诉它（它在一个不联网的
        临时页面上读）；不给的话导出里就没有这些源，存回去会把本地存储冲掉。
        """
        names = sorted(s.name for s in sites)
        if not names:
            return StateBundle()
        path = f"{STATE_DIR}/export-{uuid4().hex}.json"
        args = ["state", "export", "--sites", ",".join(names)]
        if wanted := sorted(set(origins)):
            args += ["--origins", ",".join(wanted)]
        await self.call(ref, [*args, "--out", path])
        return StateBundle.loads(await self._take(ref, path))

    async def forget(self, ref: SandboxRef, site: Site) -> None:
        await self.call(ref, ["state", "forget", site.name])

    async def takeover_open(self, ref: SandboxRef, takeover_id: str, url: str) -> None:
        """记下此刻的 cookie（接管前的样子），开接管窗口打开 url。同一个接管号再开只把窗口置前。"""
        await self.call(ref, ["takeover", "open", takeover_id, url])

    async def takeover_close(self, ref: SandboxRef, takeover_id: str) -> Captured:
        """关掉接管窗口，比较前后：变了的域名和浏览器此刻的登录态。"""
        path = f"{STATE_DIR}/takeover-{uuid4().hex}.json"
        await self.call(ref, ["takeover", "close", takeover_id, "--out", path])
        data = json.loads(await self._take(ref, path))
        return Captured(
            domains=tuple(str(d) for d in data.get("domains") or ()),
            state=StateBundle.parse(data.get("state")),
        )

    async def clipboard_set(self, ref: SandboxRef, text: str) -> None:
        """把文字放进虚拟屏幕的剪贴板（人在画面里粘贴）。

        ★ 经文件传：粘贴的常常是密码，不进命令行。
        """
        path = f"{STATE_DIR}/clipboard-{uuid4().hex}.txt"
        await self._upload(ref, path, text.encode())
        await self.call(ref, ["clipboard", "set", path])

    async def clipboard_get(self, ref: SandboxRef) -> str:
        """虚拟屏幕剪贴板里的文字（人在画面里复制）。没复制过是空串。"""
        path = f"{STATE_DIR}/clipboard-{uuid4().hex}.txt"
        await self.call(ref, ["clipboard", "get", "--out", path])
        return (await self._take(ref, path)).decode(errors="replace")

    # ------------------------------------------------------------------ 内部

    async def call(
        self, ref: SandboxRef, args: list[str], *, timeout_s: float | None = None
    ) -> dict[str, Any]:
        """执行一条平台命令，等它结束，返回 stdout 最后一行的 JSON。"""
        sid = ref.sandbox_uuid
        limit = timeout_s or self.CALL_TIMEOUT_S
        command = shlex.join([DAEMON, *args])
        cmd_id = await self._exec.spawn(
            sid, CommandSpec(command, cwd="/tmp", timeout_s=int(limit) + 5)
        )
        deadline = time.monotonic() + limit
        try:
            while (status := await self._exec.status(sid, cmd_id)).running:
                if time.monotonic() > deadline:
                    await self._exec.interrupt(sid, cmd_id)
                    raise DaemonError(f"浏览器守护的命令 {args[0]} 超时")
                await asyncio.sleep(self._poll_s)
            output = await self._output(ref, cmd_id)
        except CommandGone as exc:
            raise DaemonError(f"浏览器守护的命令 {args[0]} 没有结果（沙箱重启过）") from exc
        result = _last_json(output)
        if status.exit_code != 0:
            reason = result.get("error") or output.decode(errors="replace")[-300:].strip()
            raise DaemonError(f"浏览器守护的命令 {' '.join(args[:2])} 失败：{reason}")
        return result

    async def _output(self, ref: SandboxRef, cmd_id: str) -> bytes:
        out, cursor = b"", 0
        while True:
            chunk = await self._exec.read(ref.sandbox_uuid, cmd_id, cursor)
            if not chunk.data:
                return out
            out, cursor = out + chunk.data, chunk.cursor

    async def _upload(self, ref: SandboxRef, path: str, data: bytes) -> None:
        files = await self._exec.open_files(ref.sandbox_uuid)
        try:
            await asyncio.to_thread(files.mkdir, STATE_DIR)
            await asyncio.to_thread(files.upload, path, data)
        finally:
            files.close()

    async def _take(self, ref: SandboxRef, path: str) -> bytes:
        """取走守护写的文件，随即删掉（登录态不在本地盘上多留）。"""
        files = await self._exec.open_files(ref.sandbox_uuid)
        try:
            data = await asyncio.to_thread(files.download, path)
            try:
                await asyncio.to_thread(files.remove, [path])
            except Exception:
                logger.warning("删沙箱 %s 里的登录态文件 %s 失败", ref.sandbox_uuid, path)
        finally:
            files.close()
        if data is None:
            raise DaemonError("浏览器守护没有写出结果文件")
        return data


def _last_json(output: bytes) -> dict[str, Any]:
    """stdout 最后一行 JSON；没有就是空。"""
    for line in reversed(output.decode(errors="replace").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return {}
