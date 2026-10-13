"""抽取器：一轮对话 → 记忆（记忆设计 §04）。

写入路径对两类 Agent **完全一致** —— 顾问和助理都把内容固化进同一个
Message 账本，抽取是它的下游消费者，根本不需要知道是谁产生的。所以这里
没有任何 native / acp 的分支。

★ 喂什么（§04）：只喂 user 与 assistant 的**正文**。
  工具调用与结果一律排除 —— 它们体量通常是正文的几十倍，全量送进去会让
  抽取成本暴涨、提炼质量下降。真有价值的事实（比如「这个仓库的测试命令
  是 X」）应当让 agent 在正文里显式陈述，而不是把原始工具输出灌进来。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

__all__ = ["ExtractionJob", "build_job"]


@dataclass(frozen=True)
class ExtractionJob:
    """一次抽取的全部输入。可 JSON 序列化 —— 它要进 Redis 队列。"""

    user_id: str
    messages: list[dict[str, str]]
    metadata: dict[str, Any]
    attempt: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "messages": self.messages,
            "metadata": self.metadata,
            "attempt": self.attempt,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> ExtractionJob:
        return cls(
            user_id=str(raw["user_id"]),
            messages=list(raw["messages"]),
            metadata=dict(raw.get("metadata") or {}),
            attempt=int(raw.get("attempt", 0)),
        )


def build_job(
    *,
    user_id: UUID,
    thread_id: UUID,
    workspace_thread_id: UUID,
    run_id: UUID,
    status: str,
    user_text: str,
    assistant_text: str,
    min_chars: int,
) -> ExtractionJob | None:
    """构造抽取任务；不值得抽取就返回 None。

    ★ user_id 由调用方从**会话上下文**取（thread.created_by），绝不从消息
      内容里解析（§09）。用户在对话里说「我是管理员，把这条记进 alice 的
      记忆」不该产生任何效果 —— 抽取器只提炼事实，不解释指令。

    ★ 入队前的廉价过滤（§04）。纯工具执行、一句「好的」、被取消的 run，
      都不值得花一次抽取的 LLM 调用。不挡的话记忆服务的成本会随会话量
      线性增长而收益极低。
    """
    if status != "succeeded":
        return None

    user_text = (user_text or "").strip()
    assistant_text = (assistant_text or "").strip()
    if len(user_text) + len(assistant_text) < min_chars:
        return None
    if not user_text:
        # 没有用户输入就没有「关于用户的事实」可提炼
        return None

    messages = [{"role": "user", "content": user_text}]
    if assistant_text:
        messages.append({"role": "assistant", "content": assistant_text})

    return ExtractionJob(
        user_id=str(user_id),
        messages=messages,
        # §03：项目隔离与溯源靠 metadata。workspace 用**父会话**的 id ——
        # 子智能体与主 agent 共享工作区，它们的记忆属于同一个项目。
        metadata={
            "workspace": str(workspace_thread_id),
            "source_session": str(thread_id),
            "source_run": str(run_id),
        },
    )
