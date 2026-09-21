"""Mem0 客户端（记忆设计 §03）。

★ 作用域只有 user_id。`agent_id` / `run_id` 一律不传：

  · agent_id —— 跨用户的 agent 知识走 RAG，不走记忆。§01 论证过那是个
    跨用户投毒面：用户 B 在正常对话里陈述一个错误事实，被抽取进 agent
    知识，用户 A 之后拿到那个错误答案，而没有任何一方处在能发现的位置上。
  · run_id —— Mem0 的 run 是一次运行，与我们的 Run 语义不同，且记忆不该
    按轮次隔离。

  项目隔离靠 metadata.workspace，不靠 run_id。

★ 本模块的几处 API 细节是**读 mem0 2.1 的源码核对的**，不是照旧文档写的：

  · `add()` 用 `user_id=` 关键字；而 `search()` / `get_all()` **没有**
    user_id 关键字，只认 `filters` 字典。这个不对称是最容易踩的坑 ——
    照 1.x 的写法传 user_id 给 search 会被 **kwargs 悄悄吞掉，变成
    「查了全库」。
  · mem0 自带 `deepseek` LLM provider 与 `fastembed` embedder，不必绕
    openai 兼容层。
  · mem0 会主动剥离调用方 metadata 里的 user_id/agent_id/run_id
    （`_strip_identity_keys`），所以我们的 metadata 不可能反过来改写作用域。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any
from uuid import UUID

from .instructions import EXTRACTION_INSTRUCTIONS

if TYPE_CHECKING:
    from ..config import Settings

logger = logging.getLogger(__name__)

__all__ = ["MemoryClient", "make_memory"]


def make_memory(settings: Settings) -> MemoryClient | None:
    """按配置构造记忆客户端；没开就返回 None。

    ★ 返回 None 不是降级到某个假实现 —— 与 make_workspace 同一条纪律：
      回落会让上层以为记忆在工作，而实际上什么都没记。调用方拿到 None
      就该什么都不做。
    """
    if not settings.memory_enabled:
        return None
    return MemoryClient(settings)


class MemoryClient:
    """Mem0 的薄封装。构造很贵（要加载 embedding 模型），所以进程级一个。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._mem: Any = None

    # ------------------------------------------------------------------ 构造

    def _config(self) -> dict[str, Any]:
        s = self._settings
        key = (
            s.memory_llm_api_key.get_secret_value()
            if s.memory_llm_api_key
            else s.litellm_key.get_secret_value()
        )
        return {
            # 抽取用的模型。★ 走 DeepSeek 自己的端点，不是平台那条 Anthropic
            #   兼容路径 —— mem0 的 deepseek provider 认的是前者。
            "llm": {
                "provider": "deepseek",
                "config": {
                    "model": s.memory_llm_model,
                    "api_key": key,
                    "deepseek_base_url": s.memory_llm_base_url,
                },
            },
            # ★ FastEmbed：本地 ONNX，不需要任何 embedding 的 key。
            #   DeepSeek 没有 embeddings 接口（实测 /v1/embeddings 返回 404），
            #   而向量检索必须要 embedder —— 这是选它的直接原因。
            "embedder": {
                "provider": "fastembed",
                "config": {"model": s.memory_embed_model},
            },
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "url": s.memory_qdrant_url,
                    "collection_name": s.memory_collection,
                    # ★ 必须显式给维度。mem0 **不会**从 embedder 推断它 ——
                    #   qdrant 的默认是 1536（OpenAI 的维度），而 bge-small-zh
                    #   是 512。不给的话集合会按 1536 建，之后每次写入都报
                    #   `Vector dimension error: expected dim: 1536, got 512`。
                    #   实测踩过。
                    # ★ 从 fastembed 的模型表查而不是写死：写死的话换模型时
                    #   必然忘记同步，而症状出现在运行时的写入路径上。
                    "embedding_model_dims": _dims_of(s.memory_embed_model),
                },
            },
            "history_db_path": s.memory_history_db,
            # ★ 提炼口径的校正（记忆设计 §04 的落地）。在 mem0 的
            #   additive extraction 提示词里，custom_instructions 被标为
            #   「User-defined rules (highest priority)」—— 这是我们能
            #   压过它内置行为的唯一杠杆。见 instructions.py 的论证。
            "custom_instructions": EXTRACTION_INSTRUCTIONS,
        }

    async def _ensure(self) -> Any:
        """懒构造。第一次会加载 embedding 模型（91MB 的 ONNX），所以不在
        进程启动时做 —— 否则服务起不来的原因会变成「在加载模型」。

        ★ from_config **不是协程**（add / get_all / delete 才是）。
          对它 await 会得到 `object AsyncMemory can't be used in 'await'
          expression` —— 实测踩过。而且因为抽取是旁路、异常被吞掉，这个
          错误只在日志里，会话照常成功：属于「以为在记其实没记」。

        ★ 构造放进 to_thread：它要把 ONNX 模型读进内存并连 Qdrant，
          在事件循环里同步做会卡住整个进程。
        """
        if self._mem is None:
            import asyncio
            import os

            # ★ 关掉 mem0 的自带遥测。它默认往 PostHog（us.i.posthog.com）
            #   上报使用数据，而这是个自托管平台 —— 出站上报既是隐私问题，
            #   也是受限网络里的一个无谓超时源（实测日志里能看到它）。
            #
            #   必须在 import mem0 **之前**设：那个开关是模块导入时读的，
            #   import 之后再改不生效。
            #   用 setdefault 而不是硬写：真想开的人仍然可以用环境变量开。
            os.environ.setdefault("MEM0_TELEMETRY", "False")

            from mem0 import AsyncMemory

            self._mem = await asyncio.to_thread(AsyncMemory.from_config, self._config())
        return self._mem

    # ------------------------------------------------------------------ 写

    async def add(
        self,
        messages: list[dict[str, str]],
        *,
        user_id: UUID,
        metadata: dict[str, Any],
    ) -> Any:
        """提炼并写入。

        ★ infer=True 是重点：Mem0 内部会调一次 LLM 提炼事实、与既有记忆
          比对、决定新增/更新/忽略。关掉它就只是把 Mem0 当键值存储用，
          等于放弃了选它的全部理由（§05）。
        """
        mem = await self._ensure()
        return await mem.add(
            messages,
            user_id=str(user_id),
            metadata=metadata,
            infer=True,
        )

    # ------------------------------------------------------------------ 读

    async def get_all(self, *, user_id: UUID, limit: int = 50, **extra: Any) -> list[dict]:
        """列出某人的记忆。

        ★ user_id 必须放进 filters —— get_all 没有 user_id 关键字，
          传了会被 **kwargs 吞掉，结果是**不带作用域地查全库**。
        """
        mem = await self._ensure()
        filters: dict[str, Any] = {"user_id": str(user_id), **extra}
        result = await mem.get_all(filters=filters, top_k=limit)
        return _rows(result)

    async def search(
        self, query: str, *, user_id: UUID, top_k: int = 5, threshold: float = 0.5
    ) -> list[dict]:
        """语义检索。★ 与 get_all 同理：user_id 只能放 filters。

        ★ 本轮**不向任何 Agent 暴露**它 —— 它只服务于收敛层（converge.py）。
          检索工具是文档 §12 上线顺序的第二步，要等提炼质量达标。
        """
        mem = await self._ensure()
        result = await mem.search(
            query, filters={"user_id": str(user_id)}, top_k=top_k, threshold=threshold
        )
        return _rows(result)

    async def complete_json(self, prompt: str) -> str:
        """借 mem0 已配好的 LLM 要一段 JSON —— 收敛层用。

        ★ 复用 mem0 的 llm 而不是自己再建一个：抽取与收敛应当用同一个模型
          与同一套凭据，两份配置必然漂移。
        ★ generate_response **不是协程**（与 from_config 同一类坑），所以
          丢进 to_thread，别卡住事件循环。
        """
        import asyncio

        mem = await self._ensure()
        return await asyncio.to_thread(
            mem.llm.generate_response,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )

    async def delete(self, memory_id: str) -> None:
        mem = await self._ensure()
        await mem.delete(memory_id)


def _dims_of(model_name: str) -> int:
    """查 fastembed 的模型元数据拿向量维度 —— 只读表，不加载模型。"""
    from fastembed import TextEmbedding

    for meta in TextEmbedding.list_supported_models():
        if meta.get("model") == model_name:
            return int(meta["dim"])
    msg = f"fastembed 不认识 embedding 模型 {model_name!r}"
    raise ValueError(msg)


def _rows(result: Any) -> list[dict]:
    """mem0 的返回在不同版本里有时是 {"results": [...]}、有时是裸列表。"""
    if isinstance(result, dict):
        return list(result.get("results") or [])
    return list(result or [])
