"""浏览器组件的装配（组合根用）：按配置把登录态、宿主装好，并把附加件登记到沙箱的通用供给上。

    BrowserComponent.attach(settings, sandbox)   哪里装了沙箱组件（一段 run、一次维护、一个请求），
                                                 就在哪里接上浏览器：没配置浏览器返回 None
    refresh_in_background(settings, thread)      段末、轮末异步刷新登录态
                                                 （自己开 Redis，不占这一段的）

★ 沙箱组件不认识浏览器：这里往它的通用供给登记附加件，依赖只有浏览器 → 沙箱一个方向。
★ 构造不做 IO。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..db.session import get_sessionmaker
from ..runtime_config import RuntimeConfig
from .daemon import DaemonClient, ViewKey
from .host import BrowserAddon, BrowserHosts, CliBrowserHost, HostRegistry, NativeBrowserHost
from .login import KeyRing, LoginVault, StateCipher

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from ..config import Settings
    from ..db.models import Thread
    from ..sandbox.component import SandboxComponent

__all__ = ["BrowserComponent", "browser_configured", "refresh_in_background"]

logger = logging.getLogger(__name__)

#: 后台刷新任务（持有引用，防止被回收）
_BACKGROUND: set[asyncio.Task[Any]] = set()


def browser_configured(settings: Settings) -> bool:
    """部署配了登录态密钥和画面密钥：浏览器能力可以开。"""
    return bool(settings.browser_state_keys) and bool(
        settings.browser_view_secret.get_secret_value()
    )


@dataclass
class BrowserComponent:
    vault: LoginVault
    registry: HostRegistry
    daemon: DaemonClient
    keys: ViewKey
    addon: BrowserAddon
    hosts: BrowserHosts
    #: 沙箱里画面服务的端口
    view_port: int

    @classmethod
    def attach(
        cls,
        settings: Settings,
        sandbox: SandboxComponent,
        *,
        sessions: async_sessionmaker[AsyncSession] | None = None,
    ) -> BrowserComponent | None:
        if not browser_configured(settings):
            return None
        sessions = sessions or get_sessionmaker()
        vault = LoginVault(sessions, StateCipher(KeyRing(settings.browser_state_keys)))
        registry = HostRegistry(sessions)
        daemon = DaemonClient(sandbox.exec)
        keys = ViewKey(settings.browser_view_secret)
        addon = BrowserAddon(daemon, vault, registry, keys)
        sandbox.provisioning.register_addon(addon)
        config = RuntimeConfig(settings, sessions)
        hosts = BrowserHosts(
            native=NativeBrowserHost(sandbox.native),
            cli=CliBrowserHost(sandbox.provisioning),
            registry=registry,
            addon=addon,
            daemon=daemon,
            config=config.browser_config,
        )
        return cls(vault, registry, daemon, keys, addon, hosts, settings.browser_view_port)


def refresh_in_background(settings: Settings, thread: Thread) -> None:
    """异步导出这个会话所用浏览器里、这个人存过的站点的最新登录态。不拖慢这一轮，失败只记日志。

    ★ 自己开 Redis、自己装沙箱组件：这一段的资源在段结束后就关了。
    """

    async def work() -> None:
        from ..redisx import make_redis
        from ..sandbox.component import SandboxComponent

        redis = make_redis(settings)
        try:
            browser = BrowserComponent.attach(settings, SandboxComponent.build(settings, redis))
            if browser is not None:
                await browser.hosts.refresh(thread)
        except Exception:
            logger.warning("会话 %s 刷新登录态失败", thread.thread_uuid, exc_info=True)
        finally:
            await redis.aclose()

    task = asyncio.get_running_loop().create_task(work())
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
