"""统一生成（doc/rag-eval-design.html §8）：取平台的检索结果，用同一提示词、同一模型生成回答。

排除了各平台提示词的差异，比较的是检索链路本身；平台原生问答模式在 P3 加入。
"""

from __future__ import annotations

from .llm import LlmClient
from .schema import Answer, Chunk

__all__ = ["REFUSAL_TEXT", "generate_answer", "render_context"]

#: 资料不足时要求模型使用的固定说法（拒答判断以裁判为准，这里只是引导）
REFUSAL_TEXT = "根据现有资料无法回答"

_SYSTEM = (
    "你是企业知识库问答助手。只依据下面提供的参考资料回答用户问题，并用 [编号] 标注出处。"
    f"如果参考资料不足以回答，直接回答「{REFUSAL_TEXT}」，不要编造，也不要用常识补充。"
    "回答简洁，直接给出结论；需要计算时写出计算过程。"
)


def render_context(chunks: list[Chunk]) -> str:
    parts = []
    for i, c in enumerate(chunks, 1):
        where = " > ".join(c.title_path)
        title = f"[{i}] 《{c.doc_name or '未命名文档'}》{(' ' + where) if where else ''}"
        parts.append(f"{title}\n{c.text.strip()}")
    return "\n\n".join(parts) if parts else "（没有检索到任何资料）"


async def generate_answer(llm: LlmClient, model: str, question: str, chunks: list[Chunk]) -> Answer:
    res = await llm.chat(
        model,
        [
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": f"参考资料：\n{render_context(chunks)}\n\n问题：{question}",
            },
        ],
        max_tokens=4096,
    )
    return Answer(
        text=res.text,
        latency_ms=res.latency_ms,
        prompt_tokens=res.prompt_tokens,
        completion_tokens=res.completion_tokens,
    )
