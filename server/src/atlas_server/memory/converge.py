"""收敛：把被取代的旧记忆清掉（记忆设计 §12 第 2 项）。

## 为什么必须自己做

mem0 2.1 的抽取是 **additive extraction**，它的系统提示词第一句就是

    Your sole operation is ADD

也就是说它**结构性地只增不改**。经典算法里那套 ADD/UPDATE/DELETE 决策
（`DEFAULT_UPDATE_MEMORY_PROMPT` / `get_update_memory_messages`）在 2.1
里已经是死代码 —— 全仓没有任何调用方，`MemoryConfig.version` 也只是个
遥测用的 API 版本号，切不回旧算法。

实测后果：用户说「我改用 bun 了」之后，库里同时存在

    • 用户的包管理器从 pnpm 改为 bun …
    • User uses pnpm as their package manager and does not use npm

两条互相矛盾。将来开自动注入时，模型每轮会同时看到它们。

设计文档 §12 第 2 项问的正是「要不要在抽取器外面再加一层收敛」——
答案是要。这就是那一层。

## 取舍

★ 只在**产生了新事实**时才跑，所以没有新记忆的轮次一分钱不花。
★ 判定交给 LLM，但把它约束得很窄：只允许在「新事实直接取代旧事实」时
  返回旧 id。模棱两可一律保留 —— 误删用户记忆比多留一条矛盾记忆糟得多，
  而用户自己能在管理界面看到并删掉多余的那条（§10）。
★ 每次删除都打日志，含新旧两条原文。自动删用户数据必须留下痕迹。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from .client import MemoryClient

logger = logging.getLogger(__name__)

__all__ = ["converge"]

#: 找候选旧记忆时的相关度门槛。调高一点 —— 宁可漏掉也不要误删。
_THRESHOLD = 0.6
#: 每条新记忆最多考察多少条旧记忆。
_TOP_K = 5

_PROMPT = """\
你在维护一个用户记忆库。下面给出**刚刚新增**的记忆，以及库里与它相关的
**已有**记忆。请判断哪些已有记忆已经被新记忆取代。

判定标准（从严）：
- 只有当新记忆与某条已有记忆陈述的是**同一件事**、且新记忆的内容**直接
  推翻或更新**了它时，才算取代。
- 补充、细化、相关但不冲突 —— 都**不算**取代，保留。
- 拿不准就保留。误删用户的记忆比留下一条冗余记忆严重得多。

只返回 JSON，格式：{"superseded_ids": ["<id>", ...]}。没有则返回空数组。

## 新增的记忆
%(new)s

## 已有的相关记忆
%(old)s
"""


async def converge(memory: MemoryClient, *, user_id: UUID, added: list[dict]) -> int:
    """删掉被 `added` 取代的旧记忆，返回删除条数。

    ★ 整体吞异常：收敛是记忆的锦上添花，失败不该让抽取算作失败而进重试
      队列 —— 那会让同一批事实被反复写入。
    """
    new_texts = [str(m.get("memory", "")) for m in added if m.get("memory")]
    new_ids = {str(m.get("id")) for m in added}
    if not new_texts:
        return 0

    try:
        candidates = await _candidates(memory, user_id=user_id, new_texts=new_texts,
                                       exclude=new_ids)
        if not candidates:
            return 0
        superseded = await _judge(memory, new_texts=new_texts, candidates=candidates)
        removed = 0
        for mid in superseded:
            old = candidates.get(mid)
            if old is None:
                continue  # 模型编了个不存在的 id —— 忽略，不去猜它想删哪条
            await memory.delete(mid)
            removed += 1
            logger.info("记忆收敛：删除被取代的旧记忆 id=%s 原文=%r", mid, old[:120])
        return removed
    except Exception:
        logger.warning("记忆收敛失败，保留全部既有记忆", exc_info=True)
        return 0


async def _candidates(
    memory: MemoryClient, *, user_id: UUID, new_texts: list[str], exclude: set[str]
) -> dict[str, str]:
    """按语义找出可能被取代的旧记忆。id → 原文。"""
    found: dict[str, str] = {}
    for text in new_texts:
        for row in await memory.search(
            text, user_id=user_id, top_k=_TOP_K, threshold=_THRESHOLD
        ):
            mid = str(row.get("id"))
            if mid and mid not in exclude:
                found[mid] = str(row.get("memory", ""))
    return found


async def _judge(
    memory: MemoryClient, *, new_texts: list[str], candidates: dict[str, str]
) -> list[str]:
    """问一次模型：哪些旧的被取代了。"""
    prompt = _PROMPT % {
        "new": "\n".join(f"- {t}" for t in new_texts),
        "old": "\n".join(f"- [{k}] {v}" for k, v in candidates.items()),
    }
    raw = await memory.complete_json(prompt)
    data = json.loads(raw) if isinstance(raw, str) else (raw or {})
    ids = data.get("superseded_ids") or []
    return [str(i) for i in ids if isinstance(i, str | int)]
