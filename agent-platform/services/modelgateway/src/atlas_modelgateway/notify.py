"""配置变了要告诉网关：改账号、模型、线路、Key、授权的事务提交后，网关状态里的配置版本号加一。

    notify.mark(session)        服务里改了配置就标一下
    await notify.flush(session) 会话提交之后调用（db/session.get_session 里）

★ 提交之后才加一：网关看到新版本号去重新加载时，读到的一定是提交后的数据。
★ 网关状态没配（本机、测试）就什么都不做；网关按时间兜底重新加载。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from .gateway.config import VersionWatch

__all__ = ["flush", "mark", "use_redis"]

logger = logging.getLogger(__name__)

_redis: Any | None = None
_KEY = "config_changed"


def use_redis(redis: Any | None) -> None:
    global _redis
    _redis = redis


def mark(session: AsyncSession) -> None:
    session.info[_KEY] = True


async def flush(session: AsyncSession) -> None:
    if not session.info.pop(_KEY, False) or _redis is None:
        return
    try:
        await VersionWatch.bump(_redis)
    except Exception:
        logger.warning("配置版本号加一失败，网关最晚按时间兜底重新加载", exc_info=True)
