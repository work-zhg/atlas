"""评测流程测试：出处匹配、检索指标、数据集、跑题 → 判分 → 报告（假平台 + 假模型，不连网络）。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from atlas_bench.datasets import find_dataset, load_dataset
from atlas_bench.llm import LlmClient
from atlas_bench.platforms.base import IngestReport, KbHandle
from atlas_bench.report import write_report
from atlas_bench.runner import run_experiment
from atlas_bench.schema import Chunk, Corpus, ExperimentConfig, Question
from atlas_bench.score import score_run
from atlas_bench.scoring.matching import match_evidence, normalize
from atlas_bench.scoring.retrieval import score_retrieval
from atlas_bench.settings import BENCH_DIR, BenchSettings
from atlas_bench.stats import estimate, paired_diff

DEMO = BENCH_DIR / "datasets" / "demo-zh"


# ───────────────────────────────────────────── 出处匹配


def test_normalize_ignores_width_space_and_table_bars() -> None:
    assert normalize("| 一线城市 | 北京、上海 | 600 元 |") == normalize(
        "一线城市　北京，上海 600元"
    )
    assert normalize("ＡＢＣ １２") == "abc12"


def test_match_full_partial_and_miss() -> None:
    chunks = ["### 四、住宿标准\n| 一线城市 | 北京、上海、广州、深圳 | 600 元 |", "其他内容"]
    m = match_evidence(
        ["| 一线城市 | 北京、上海、广州、深圳 | 600 元 |", "完全不相关的出处文字"], chunks
    )
    assert (m[0].rank, m[0].kind) == (1, "full")
    assert (m[1].rank, m[1].kind) == (None, "miss")
    # 出处被分段边界切开：相邻两段拼起来包含 → 部分命中
    split = ["前文……单次出差预算超过 10000 元的，", "须经部门总监审批；后文……"]
    (p,) = match_evidence(["单次出差预算超过 10000 元的，须经部门总监审批"], split)
    assert (p.rank, p.kind) == (1, "partial")


def test_match_passage_mode_requires_equality() -> None:
    # 短标题被别的段包含：按片段判会在第 1 名误命中，按整段只认第 2 名
    chunks = ["苹果手机壳 透明防摔款", "苹果手机壳"]
    assert match_evidence(["苹果手机壳"], chunks)[0].rank == 1
    (m,) = match_evidence(["苹果手机壳"], chunks, mode="passage")
    assert (m.rank, m.kind) == (2, "full")
    (miss,) = match_evidence(["苹果手机"], chunks, mode="passage")
    assert (miss.rank, miss.kind) == (None, "miss")


def _q(qid: str, quotes: list[str], **kw: Any) -> Question:
    from atlas_bench.schema import Evidence

    return Question(
        id=qid, question=qid, evidence=tuple(Evidence(doc_id="d", quote=t) for t in quotes), **kw
    )


def test_retrieval_metrics_match_hand_calculation() -> None:
    qs = {
        "a": _q("a", ["甲甲甲甲甲"]),  # 第 2 名命中 → hit 1, mrr 0.5
        "b": _q("b", ["乙乙乙乙乙", "丙丙丙丙丙"]),  # 只命中一条（第 1 名）→ recall 0.5
        "c": _q("c", ["丁丁丁丁丁"]),  # 没命中
        "d": _q("d", [], unanswerable=True),  # 拒答题：不计检索指标
    }
    rows = {
        "a": {
            "retrieval": {"chunks": [{"text": "无关"}, {"text": "甲甲甲甲甲"}, {"text": "无关2"}]}
        },
        "b": {
            "retrieval": {"chunks": [{"text": "乙乙乙乙乙"}, {"text": "无关"}, {"text": "无关2"}]}
        },
        "c": {"retrieval": {"chunks": [{"text": "x"}, {"text": "y"}, {"text": "z"}]}},
        "d": {"retrieval": {"chunks": [{"text": "x"}]}},
    }
    s = score_retrieval(qs, rows, top_k=3)
    assert s["a"]["hit"] == 1 and s["a"]["mrr"] == pytest.approx(0.5) and s["a"]["first_rank"] == 2
    assert s["b"]["hit"] == 1 and s["b"]["recall"] == pytest.approx(0.5) and s["b"]["mrr"] == 1
    assert s["c"]["hit"] == 0 and s["c"]["mrr"] == 0 and s["c"]["ndcg"] == 0
    assert "hit" not in s["d"] and s["d"]["context_chars"] == 1
    # nDCG：单个相关文档在第 2 名 → 1 / log2(3)
    assert s["a"]["ndcg"] == pytest.approx(1 / 1.5849625, rel=1e-4)


def test_stats() -> None:
    e = estimate([1, 1, 0, 1])
    assert e.mean == 0.75 and e.low <= 0.75 <= e.high and e.n == 4
    diff, sig = paired_diff({"a": 0, "b": 0, "c": 0}, {"a": 1, "b": 1, "c": 1})
    assert diff.mean == 1 and sig


# ───────────────────────────────────────────── 数据集


def test_demo_dataset_quotes_are_verbatim() -> None:
    ds = load_dataset(DEMO)
    docs = {d.doc_id: Path(d.path).read_text(encoding="utf-8") for d in ds.corpus.documents}
    assert len(ds.questions) == 20
    for q in ds.questions:
        for e in q.evidence:
            assert e.quote in docs[e.doc_id], (q.id, e.quote)
    assert sum(q.unanswerable for q in ds.questions) == 3


def test_cmteb_write_dataset_one_passage_per_segment(tmp_path: Path) -> None:
    from atlas_bench.importers.cmteb import write_dataset

    passages = [
        ("p1", "第一段\n\n\n段内空行"),
        ("p2", "  "),  # 空段：跳过
        ("p3", "长" * 30),  # 超长：截断到 max_chars
        ("p4", "第一段\n段内空行"),  # 与 p1 清洗后相同
        ("p5", "第五段"),
    ]
    queries = [("q1", "问一"), ("q2", "问二"), ("q3", "问三")]
    qrels = [("q1", "p1", 1), ("q1", "p5", 1), ("q2", "p2", 1), ("q3", "p9", 1), ("q1", "p3", 0)]
    root = tmp_path / "cmteb-x"
    stats = write_dataset(
        root,
        name="cmteb-x",
        version="abc",
        source={"repo": "C-MTEB/X"},
        passages=passages,
        queries=queries,
        qrels=qrels,
        per_file=2,
        max_chars=20,
    )
    assert stats["passages"] == 4 and stats["files"] == 2 and stats["queries"] == 1
    assert stats["empty_passages"] == 1 and stats["truncated_passages"] == 1
    assert stats["duplicate_passages"] == 1
    assert stats["queries_skipped"] == 2 and stats["qrels_missing_passage"] == 2

    ds = load_dataset(root)
    assert ds.evidence_match == "passage" and len(ds.corpus.documents) == 2
    # 平台按 "\n\n" 切：每个文件切出来的就是写进去的段，出处与其中一段完全相同
    pieces = {
        d.doc_id: [t.strip() for t in Path(d.path).read_text(encoding="utf-8").split("\n\n")]
        for d in ds.corpus.documents
    }
    assert sum(len(v) for v in pieces.values()) == 4
    assert all(len(t) <= 20 for v in pieces.values() for t in v)
    (q,) = ds.questions
    assert q.id == "q1" and q.task == "retrieval" and not q.unanswerable
    assert [e.passage_id for e in q.evidence] == ["p1", "p5"]
    for e in q.evidence:
        assert e.quote in pieces[e.doc_id]
    with pytest.raises(FileExistsError):
        write_dataset(
            root, name="cmteb-x", version="abc", source={}, passages=[], queries=[], qrels=[]
        )


# ───────────────────────────────────────────── 端到端（假平台 + 假模型）


class FakePlatform:
    """把文档按 ## 小节切开，按与问题的公共字符数排序 —— 够检验流程，不追求效果。"""

    name = "atlas"

    def __init__(self) -> None:
        self.ingested = 0
        self.sections: list[tuple[str, str]] = []

    async def describe(self) -> dict[str, Any]:
        return {"base_url": "fake://atlas"}

    async def ingest(self, corpus: Corpus, ingest: dict[str, Any]) -> KbHandle:
        self.ingested += 1
        for d in corpus.documents:
            for part in Path(d.path).read_text(encoding="utf-8").split("\n## "):
                self.sections.append((d.doc_id, part))
        return KbHandle("atlas", f"kb{self.ingested}", "bench:fake")

    async def wait_ready(self, kb: KbHandle, *, timeout_s: float) -> IngestReport:
        return IngestReport(3, [], 0.0)

    async def retrieve(
        self, kb: KbHandle, question: str, *, top_k: int, retrieval: dict[str, Any]
    ) -> list[Chunk]:
        ranked = sorted(self.sections, key=lambda s: -len(set(question) & set(s[1])))
        return [Chunk(text=t, score=1.0, doc_name=d) for d, t in ranked[:top_k]]

    async def cleanup(self, kb: KbHandle) -> None: ...

    async def aclose(self) -> None: ...


@dataclass
class _Msg:
    content: str


@dataclass
class _Choice:
    message: _Msg


@dataclass
class _Usage:
    prompt_tokens: int = 100
    completion_tokens: int = 20


class _Resp:
    def __init__(self, text: str) -> None:
        self.choices = [_Choice(_Msg(text))]
        self.usage = _Usage()


class FakeOpenAI:
    """生成：拒答题回答「无法回答」，其余照抄资料第一段；裁判：看回答里有没有「无法回答」。"""

    def __init__(self) -> None:
        self.chat = self
        self.completions = self
        self.calls = 0

    async def create(self, **params: Any) -> _Resp:
        self.calls += 1
        user = params["messages"][-1]["content"]
        if params.get("response_format"):
            refused = "无法回答" in user.split("待评回答：")[-1]
            no_answer = "没有答案" in user
            verdict = (
                "correct" if (refused == no_answer) else ("missing" if refused else "incorrect")
            )
            return _Resp(json.dumps({"refused": refused, "verdict": verdict, "reason": "fake"}))
        if any(k in user for k in ("加班费", "持股", "预借")):
            return _Resp("根据现有资料无法回答")
        return _Resp(user.split("\n")[2][:80])


async def test_run_score_report_end_to_end(tmp_path: Path) -> None:
    settings = BenchSettings(runs_dir=tmp_path / "runs", cache_path=tmp_path / "cache.sqlite")
    cfg = ExperimentConfig.model_validate(
        {
            "name": "test",
            "datasets": ["demo-zh"],
            "split": "all",
            "top_k": 3,
            "generation_metrics": ["crag", "refusal"],  # 不调 RAGAS（要真实模型）
            "platforms": [
                {
                    "adapter": "atlas",
                    "variants": {
                        "a": {"ingest": {"doc_form": "hierarchical_model"}, "retrieval": {}},
                        "b": {
                            "ingest": {"doc_form": "hierarchical_model"},
                            "retrieval": {"search_method": "hybrid_search"},
                        },
                    },
                }
            ],
        }
    )
    fake_llm = FakeOpenAI()
    llm = LlmClient("fake://llm", "k", cache_path=settings.cache_path, client=fake_llm)
    platform = FakePlatform()
    run_dir = await run_experiment(cfg, settings, platforms={"atlas": platform}, llm=llm, limit=6)

    # 两个配置的入库配置相同 → 只入库一次
    assert platform.ingested == 1
    raw = (run_dir / "raw" / "demo-zh" / "atlas__a.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(raw) == 6 and all("generations" in json.loads(r) for r in raw)

    # 断点续跑：再跑一次不重复
    calls = fake_llm.calls
    await run_experiment(
        cfg, settings, platforms={"atlas": platform}, llm=llm, run_dir=run_dir, limit=6
    )
    assert fake_llm.calls == calls
    assert (
        len(
            (run_dir / "raw" / "demo-zh" / "atlas__a.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        == 6
    )

    summary = await score_run(run_dir, settings, llm)
    res = summary["results"]["demo-zh/atlas/a"]
    assert res["n_questions"] == 6
    assert 0 <= res["retrieval"]["hit"]["mean"] <= 1
    assert res["generation"]["crag"]["n"] == 6
    assert sum(res["attribution"].values()) == 6

    # 重新判分走缓存，不再调用裁判
    calls = fake_llm.calls
    await score_run(run_dir, settings, llm)
    assert fake_llm.calls == calls

    md, page = write_report(run_dir, settings)
    text = md.read_text(encoding="utf-8")
    assert "总表" in text and "demo-zh/atlas/b" in text and "与基线配置的配对比较" in text
    assert page.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_find_dataset_missing() -> None:
    with pytest.raises(FileNotFoundError):
        find_dataset("no-such", [DEMO.parent])
