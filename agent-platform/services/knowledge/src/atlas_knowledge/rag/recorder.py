"""检索过程记录：每个环节的候选（谁进来、排第几、多少分）+ 对应的 span。

一次检索在库里留一条 kb_retrieval（services/retrieval.py 落库），前端据此画「漏斗」：
某个分段在哪一环掉的 —— 没召回、重排后排名靠后被截断、还是被分数阈值滤掉。
同一份数据也挂在 span 上（数量、前几名分数；正文按档位），在 Jaeger / Langfuse 里看。

    with rec.stage("semantic", dataset_uuid=…, span_kind="RETRIEVER") as st:
        hits = await store.search_by_vector(…)
        st.set(hits)

★ 环节在进入时登记（而非结束时）：混合检索两路并发，顺序要稳定。
★ 候选只存 id / 分数 / 名次，不存正文：正文在分段表里，看漏斗时再查（已删的分段显示为已删除）。
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from opentelemetry.trace import Span

from ..telemetry import SPAN_KIND, set_content, tracer
from .models import Document
from .vdb import Hit

__all__ = ["RetrievalRecorder", "Stage"]

#: span 上只列前几名的分数（全部候选在 kb_retrieval 里）
_SPAN_TOP = 10


def _item(x: Hit | Document | Any) -> dict[str, Any]:
    if isinstance(x, Hit):
        return {
            "point": x.id,
            "segment": x.payload.get("segment_uuid"),
            "kind": x.payload.get("kind") or "segment",
            "score": round(float(x.score), 6),
        }
    if isinstance(x, Document):
        m = x.metadata
        return {
            "point": m.get("point_id"),
            "segment": m.get("segment_uuid"),
            "kind": m.get("kind") or "segment",
            "score": round(float(m.get("score") or 0.0), 6),
        }
    # RetrievedSegment（组装后是分段级）
    return {"point": None, "segment": x.segment_uuid, "kind": "segment", "score": round(x.score, 6)}


@dataclass
class Stage:
    name: str
    dataset_uuid: str | None = None
    items: list[dict[str, Any]] | None = None
    elapsed_ms: float = 0.0
    attrs: dict[str, Any] = field(default_factory=dict)
    span: Span | None = None

    def set(self, xs: Sequence[Any], **attrs: Any) -> None:
        self.items = [_item(x) for x in xs]
        self.attrs.update(attrs)
        if self.span is not None:
            self.span.set_attribute("knowledge.candidates", len(self.items))
            top = self.items[:_SPAN_TOP]
            self.span.set_attribute("knowledge.top_scores", [i["score"] for i in top])
            self.span.set_attribute("knowledge.top_segments", [str(i["segment"]) for i in top])
            for k, v in attrs.items():
                if isinstance(v, (str, int, float, bool)):
                    self.span.set_attribute(f"knowledge.{k}", v)
            # 各环节候选的正文只在 full 档进 Langfuse
            contents = [getattr(x, "page_content", None) for x in xs[:_SPAN_TOP]]
            if any(contents):
                set_content(self.span, output=contents, level="full")

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "dataset_uuid": self.dataset_uuid,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "attrs": self.attrs,
            "items": self.items or [],
        }


class RetrievalRecorder:
    def __init__(self) -> None:
        self.stages: list[Stage] = []

    @contextmanager
    def stage(
        self,
        name: str,
        *,
        dataset_uuid: str | None = None,
        span_kind: str | None = None,
        **attrs: Any,
    ) -> Iterator[Stage]:
        st = Stage(name, dataset_uuid, attrs={k: v for k, v in attrs.items() if v is not None})
        self.stages.append(st)
        with tracer().start_as_current_span(f"knowledge.{name}") as span:
            st.span = span
            if span_kind:
                span.set_attribute(SPAN_KIND, span_kind)
            if dataset_uuid:
                span.set_attribute("knowledge.dataset_uuid", dataset_uuid)
            for k, v in st.attrs.items():
                if isinstance(v, (str, int, float, bool)):
                    span.set_attribute(f"knowledge.{k}", v)
            t = time.perf_counter()
            try:
                yield st
            finally:
                st.elapsed_ms = (time.perf_counter() - t) * 1000
                st.span = None
        if st.items is None:  # 只计时、不产出候选的环节（如查询向量）不进漏斗
            self.stages.remove(st)

    def dump(self) -> list[dict[str, Any]]:
        return [s.as_dict() for s in self.stages]
