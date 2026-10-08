"""会话文件预览：令牌签发与解析（doc/detail/file-preview.html §06）。

★ 预览站点按 URL 路径取文件，iframe / img / 新标签都带不了 X-User-Id ——
  所以凭据是 URL 路径里的一个短期令牌。放在路径而不是 query：相对路径
  解析（`<script src="app.js">`）会丢掉 query。

★ 令牌存 Redis 而不是做成签名串：可吊销、不用管密钥轮换。代价是每个子资源
  一次 Redis GET，原型规模（几十个文件）无压力。

★ 解析**不碰数据库**：签发时把构造工作区要的三个 id 一并存进去。站点的每个
  子资源都会走一次解析，不该各占一条 DB 连接。
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import redis.asyncio as aioredis

from ..config import Settings
from ..errors import NotFound
from ..providers.filesystem import make_workspace
from ..providers.filesystem.oss import WorkspaceListing
from ..repositories.thread import ThreadRepository

_KEY = "atlas:preview:{}"


class PreviewService:
    def __init__(self, redis: aioredis.Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings

    async def issue(self, threads: ThreadRepository, thread_id: UUID) -> tuple[str, datetime]:
        """为一个会话签发预览令牌 → (token, 过期时间)。

        ★ 读权限现在等价于「会话存在」（决策 1：不做鉴权），与 list_files /
          download 一致。有鉴权之后在这里加 created_by == user 的检查。
        """
        found = await threads.get(thread_id)
        if found is None:
            raise NotFound(f"会话 {thread_id} 不存在", thread_id=str(thread_id))
        if not self._settings.workspace_configured:
            raise NotFound("没有配置对象存储，这个部署没有文件能力")
        thread, _ = found
        token = secrets.token_urlsafe(32)
        payload = {
            "thread_id": str(thread.id),
            # 工作区的 key 里有 user 段，取的是会话创建者而不是当前请求者
            "owner_id": str(thread.created_by),
            "workspace_thread_id": (
                str(thread.workspace_thread_id) if thread.workspace_thread_id else None
            ),
        }
        ttl = self._settings.preview_token_ttl_s
        await self._redis.set(_KEY.format(token), json.dumps(payload), ex=ttl)
        return token, datetime.now(UTC) + timedelta(seconds=ttl)

    async def resolve(self, token: str) -> WorkspaceListing | None:
        """令牌 → 工作区。失效返回 None。每次命中续期（滑动过期）。"""
        key = _KEY.format(token)
        raw = await self._redis.get(key)
        if raw is None:
            return None
        await self._redis.expire(key, self._settings.preview_token_ttl_s)
        data: dict[str, Any] = json.loads(raw)
        fs = make_workspace(
            self._settings,
            UUID(data["owner_id"]),
            UUID(data["thread_id"]),
            workspace_thread_id=(
                UUID(data["workspace_thread_id"]) if data.get("workspace_thread_id") else None
            ),
        )
        return None if fs is None else WorkspaceListing(fs)

    def base_url(self, token: str, *, request_base: str) -> str:
        """站点根地址（以 / 结尾，相对路径据此解析）。"""
        origin = (self._settings.preview_base_url or request_base).rstrip("/")
        return f"{origin}/v1/previews/{token}/"
