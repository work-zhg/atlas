"""平台适配器接口（doc/rag-eval-design.html §7）。

每个被测平台（Atlas / Dify / RAGFlow）实现同样几个动作，只调各平台的公开 HTTP 接口。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ..schema import Chunk, Corpus

__all__ = ["IngestReport", "KbHandle", "Platform"]


@dataclass
class KbHandle:
    platform: str
    kb_id: str
    name: str
    reused: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class IngestReport:
    documents: int
    failed: list[str]
    seconds: float


class Platform(Protocol):
    name: str

    async def describe(self) -> dict[str, Any]:
        """平台版本与地址，写进运行的 manifest。"""

    async def ingest(self, corpus: Corpus, ingest: dict[str, Any]) -> KbHandle:
        """建库并上传文档；同一（语料内容, 入库配置）已有完整的库就复用。"""

    async def wait_ready(self, kb: KbHandle, *, timeout_s: float) -> IngestReport: ...

    async def retrieve(
        self, kb: KbHandle, question: str, *, top_k: int, retrieval: dict[str, Any]
    ) -> list[Chunk]: ...

    async def cleanup(self, kb: KbHandle) -> None: ...

    async def aclose(self) -> None: ...
