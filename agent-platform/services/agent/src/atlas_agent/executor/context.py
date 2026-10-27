"""每轮上下文的装配：持久指令（L1）（上下文管理设计 §10）。

    持久指令  平台（管理员）→ 用户（个人偏好）→ 工作区 /workspace/ATLAS.md（兼容 AGENTS.md）
              拼在系统提示词之后；后面的优先级更高（离具体场景更近）

★ 记忆不在这里：常驻块与更新提示归 memory/runtime.py（doc/memory-design.html §5）。
★ 「锦上添花」：配置服务不可用时跳过并记日志，不让这一轮失败。
★ 指令每轮开始时读一次：本轮内不变（保住前缀缓存），修改从下一轮生效。
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from ..config import Settings
from ..configplane.client import ConfigClient

__all__ = ["load_instructions"]

logger = logging.getLogger(__name__)

#: 每层的字符上限（约 4k tokens）与总上限
_LAYER_CHARS = 10_000
_TOTAL_CHARS = 30_000
_WORKSPACE_FILES = ("/workspace/ATLAS.md", "/workspace/AGENTS.md")


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "\n…（超出长度上限，已截断）"


async def _workspace_instructions(workspace: Any) -> tuple[str, str] | None:
    if workspace is None:
        return None
    for path in _WORKSPACE_FILES:
        try:
            result = await workspace.aread(path, offset=0, limit=2_000)
        except Exception:
            continue
        data = getattr(result, "file_data", None) or {}
        content = data.get("content") if isinstance(data, dict) else None
        if isinstance(content, str) and content.strip() and not getattr(result, "error", None):
            return path, content
    return None


async def load_instructions(settings: Settings, workspace: Any, user_id: UUID | str | None) -> str:
    """拼好的持久指令段；什么都没有时返回空串。"""
    layers: list[tuple[str, str]] = []
    if settings.config_base_url and user_id:
        try:
            got = await ConfigClient(settings.config_base_url).get(
                "/internal/instructions", user_id=str(user_id)
            )
            if got.get("platform"):
                layers.append(("平台指令（管理员设定，必须遵守）", got["platform"]))
            if got.get("user"):
                layers.append(("用户的个人指令", got["user"]))
        except Exception as exc:  # 配置服务不可用：本轮不带指令，不失败
            logger.warning("读取持久指令失败，本轮跳过：%s", exc)
    if found := await _workspace_instructions(workspace):
        path, content = found
        layers.append((f"工作区约定（{path}）", content))
    if not layers:
        return ""
    parts = [f"【{title}】\n{_clip(text, _LAYER_CHARS)}" for title, text in layers]
    body = _clip("\n\n".join(parts), _TOTAL_CHARS)
    return "以下是持久指令，按顺序优先级递增（后面的覆盖前面的）：\n\n" + body
