"""每轮上下文的装配：持久指令（L1）与记忆附件（L4）（上下文管理设计 §10 / §11）。

    持久指令  平台（管理员）→ 用户（个人偏好）→ 工作区 /workspace/ATLAS.md（兼容 AGENTS.md）
              拼在系统提示词之后；后面的优先级更高（离具体场景更近）
    记忆附件  用本轮提问检索相关记忆，作为 <system-reminder> 附在这一次请求末尾 —— 不落库、
              不进历史，下一轮按下一轮的提问重新检索（与知识库 auto 模式同一做法）

★ 两者都是「锦上添花」：配置服务 / 记忆服务不可用时跳过并记日志，不让这一轮失败。
★ 指令每轮开始时读一次：本轮内不变（保住前缀缓存），修改从下一轮生效。
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from ..config import Settings
from ..configplane.client import ConfigClient
from ..domain.translator import extract_text

__all__ = ["load_instructions", "memory_attachments"]

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
        token = (
            settings.config_internal_token.get_secret_value()
            if settings.config_internal_token
            else ""
        )
        try:
            got = await ConfigClient(settings.config_base_url, token or None).get(
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


async def memory_attachments(
    settings: Settings,
    memory: Any,
    *,
    user_id: UUID,
    input_content: Any,
    thread_memory: str,
) -> list[tuple[str, str]]:
    """本轮相关的用户记忆 → [("memory", 文本)]。"""
    if memory is None or not settings.memory_auto_inject or thread_memory == "off":
        return []
    query = extract_text(input_content).strip() if input_content else ""
    if not query:  # 续跑段没有新的提问
        return []
    try:
        hits = await memory.search(
            query[:1_000],
            user_id=user_id,
            top_k=settings.memory_inject_top_k,
            threshold=settings.memory_inject_threshold,
        )
    except Exception as exc:
        logger.warning("记忆检索失败，本轮不注入：%s", exc)
        return []
    lines = []
    for h in hits:
        text = str(h.get("memory") or h.get("text") or "").strip()
        if text:
            lines.append(f"- {text}（记忆 {str(h.get('id', ''))[:8]}）")
    if not lines:
        return []
    body = _clip("\n".join(lines), settings.memory_inject_max_chars)
    return [
        (
            "memory",
            "以下是关于这位用户的历史记忆（系统从过往会话中提取，可能已过期；"
            "与持久指令或用户当前的说法冲突时，以后者为准）：\n" + body,
        )
    ]
