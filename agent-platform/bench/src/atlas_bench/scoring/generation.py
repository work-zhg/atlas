"""生成评测（doc/rag-eval-design.html §10、§11）。

    crag + refusal   一次裁判调用：是否拒答 + 对照参考答案判 correct / missing / incorrect
                     → CRAG 计分：正确 +1、没答 0、答错 −1；拒答题拒答 +1、编造答案 −1
    ragas_faithfulness         RAGAS Faithfulness：回答的陈述是否都有检索依据
    ragas_factual_correctness  RAGAS FactualCorrectness：参考答案的要点是否被回答覆盖
                               （默认 recall 模式）

★ RAGAS 的结果按（指标, RAGAS 版本, 输入哈希）缓存：重新判分不重复花钱。
★ 拒答的回答不算忠实度 / 正确性（没有可判断的陈述），只计入 CRAG 与拒答指标。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from ..llm import LlmClient
from ..schema import Question

__all__ = ["GenerationScorer", "attribute"]

logger = logging.getLogger(__name__)

_JUDGE_SYSTEM = """你是严格、公正的问答评测裁判。
根据「问题」「参考答案」判断「待评回答」，只输出 JSON：
{"refused": true 或 false, "verdict": "correct" | "incorrect" | "missing", "reason": "一句话理由"}

规则：
- refused：待评回答是否表示资料不足、无法回答、不知道（没有给出具体答案）。
- 当参考答案是「（该问题在资料中没有答案）」时：
  回答拒答 → verdict=correct；给出了具体答案 → verdict=incorrect。
- 其他情况：
  - correct：关键事实与参考答案一致（数字、日期、名称必须一致；表述不同、补充了正确的细节都可以）。
  - missing：拒答，或没有给出问题要的信息。
  - incorrect：关键事实错误、与参考答案矛盾，或编造了会改变结论的内容。
"""


class _ScoreCache:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS score (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self._db.commit()

    def get(self, key: str) -> Any:
        row = self._db.execute("SELECT value FROM score WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key: str, value: Any) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO score (key, value) VALUES (?, ?)", (key, json.dumps(value))
        )
        self._db.commit()


def _key(*parts: Any) -> str:
    return hashlib.sha256(
        json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


class GenerationScorer:
    def __init__(
        self,
        llm: LlmClient,
        judge_model: str,
        metrics: list[str],
        cache_path: Path,
        *,
        factual_mode: str = "recall",
    ) -> None:
        self._llm = llm
        self._judge = judge_model
        self._metrics = set(metrics)
        self._factual_mode = factual_mode
        self._cache = _ScoreCache(cache_path)
        self._ragas: dict[str, Any] = {}
        self._ragas_version = ""
        if self._metrics & {"ragas_faithfulness", "ragas_factual_correctness"}:
            self._init_ragas()

    def _init_ragas(self) -> None:
        import ragas
        from ragas.llms import llm_factory
        from ragas.metrics.collections import FactualCorrectness, Faithfulness

        self._ragas_version = ragas.__version__
        llm = llm_factory(self._judge, provider="openai", client=self._llm.raw)
        if "ragas_faithfulness" in self._metrics:
            self._ragas["ragas_faithfulness"] = Faithfulness(llm=llm)
        if "ragas_factual_correctness" in self._metrics:
            self._ragas["ragas_factual_correctness"] = FactualCorrectness(
                llm=llm, mode=self._factual_mode
            )

    async def _judge_answer(self, q: Question, answer: str) -> dict[str, Any]:
        reference = q.reference if not q.unanswerable else "（该问题在资料中没有答案）"
        res = await self._llm.chat_json(
            self._judge,
            [
                {"role": "system", "content": _JUDGE_SYSTEM},
                {
                    "role": "user",
                    "content": f"问题：{q.question}\n\n参考答案：{reference}\n\n待评回答：{answer}",
                },
            ],
            max_tokens=4096,
        )
        verdict = (
            res.get("verdict")
            if res.get("verdict") in ("correct", "incorrect", "missing")
            else "incorrect"
        )
        refused = bool(res.get("refused"))
        if q.unanswerable:
            crag = 1.0 if refused else -1.0
        else:
            crag = {"correct": 1.0, "missing": 0.0, "incorrect": -1.0}[verdict]
        return {
            "refused": refused,
            "verdict": verdict,
            "crag": crag,
            "judge_reason": res.get("reason", ""),
        }

    async def _ragas_score(
        self, name: str, q: Question, answer: str, contexts: list[str]
    ) -> float | None:
        metric = self._ragas[name]
        key = _key(
            name,
            self._ragas_version,
            self._judge,
            self._factual_mode,
            q.question,
            q.reference,
            answer,
            contexts,
        )
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        try:
            if name == "ragas_faithfulness":
                result = await metric.ascore(
                    user_input=q.question, response=answer, retrieved_contexts=contexts
                )
            else:
                result = await metric.ascore(response=answer, reference=q.reference)
            value = float(result.value) if result.value == result.value else None  # NaN → None
        except Exception as exc:
            logger.warning("RAGAS %s 失败（%s）：%s", name, q.id, exc)
            return None
        self._cache.put(key, value)
        return value

    async def score(self, q: Question, answer: str, contexts: list[str]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self._metrics & {"crag", "refusal"}:
            out.update(await self._judge_answer(q, answer))
        answered = not out.get("refused", False)
        if not q.unanswerable and answered:
            for name in ("ragas_faithfulness", "ragas_factual_correctness"):
                if name in self._ragas:
                    out[name] = await self._ragas_score(name, q, answer, contexts)
        return out


def attribute(q: Question, retrieval: dict[str, Any], gen: dict[str, Any]) -> str:
    """错题归因（doc/rag-eval-design.html §10）。"""
    refused = gen.get("refused")
    if q.unanswerable:
        return "正确" if refused else "该拒未拒"
    verdict = gen.get("verdict")
    if verdict == "correct":
        return "正确"
    hit = retrieval.get("hit")
    if refused:
        return "检索未命中" if hit == 0 else "误拒"
    if hit == 0:
        return "检索未命中"
    faithful = gen.get("ragas_faithfulness")
    if faithful is not None and faithful < 0.5:
        return "模型编造"
    return "理解或推理错误"
