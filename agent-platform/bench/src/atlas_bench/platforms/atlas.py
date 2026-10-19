"""Atlas 知识库的适配器：只用知识库服务的公开接口（/v1）。

    入库    POST /v1/datasets → POST /v1/datasets/{id}/documents（multipart，分批）
    等待    GET  /v1/datasets/{id}/documents，直到没有入库中的文档
    检索    POST /v1/datasets/{id}/hit-testing（可逐次指定检索设置：同一个库能评多种检索方式）
    清理    DELETE /v1/datasets/{id}

身份：内网免令牌（内网身份传递 §12）—— 请求头 X-Uid = 评测用户 atlas-bench（需 kb:create），
X-Caller-App: bench。建的库 OWNER 是它，库名 bench:<数据集>:<语料与入库配置的指纹>，评测完
可以按前缀整体清理。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import time
from pathlib import Path
from typing import Any

import httpx

from ..schema import Chunk, Corpus
from .base import IngestReport, KbHandle

__all__ = ["KB_PREFIX", "AtlasPlatform"]

KB_PREFIX = "bench:"
_RUNNING = {"waiting", "parsing", "splitting", "indexing"}
_UPLOAD_BATCH = 20


def _fingerprint(corpus: Corpus, ingest: dict[str, Any]) -> str:
    raw = corpus.content_hash + json.dumps(ingest, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


class AtlasError(RuntimeError):
    pass


class AtlasPlatform:
    name = "atlas"

    def __init__(
        self,
        base_url: str,
        headers: dict[str, str],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._c = httpx.AsyncClient(
            base_url=self._base,
            headers=headers,
            timeout=120.0,
            trust_env=False,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._c.aclose()

    async def _req(self, method: str, path: str, **kw: Any) -> Any:
        r = await self._c.request(method, path, **kw)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = r.text[:300]
            raise AtlasError(f"{method} {path} → {r.status_code}：{detail}")
        return None if r.status_code == 204 else r.json()

    async def describe(self) -> dict[str, Any]:
        meta = await self._req("GET", "/v1/meta")
        return {"base_url": self._base, "embedding_models": meta.get("embedding_models")}

    # ------------------------------------------------------------------ 入库

    async def _find(self, name: str) -> dict[str, Any] | None:
        page = await self._req("GET", "/v1/datasets", params={"keyword": name, "limit": 100})
        return next((d for d in page["data"] if d["name"] == name), None)

    async def _documents(self, kb_id: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            res = await self._req(
                "GET", f"/v1/datasets/{kb_id}/documents", params={"page": page, "limit": 100}
            )
            out.extend(res["data"])
            if len(out) >= res["total"] or not res["data"]:
                return out
            page += 1

    async def ingest(self, corpus: Corpus, ingest: dict[str, Any]) -> KbHandle:
        name = f"{KB_PREFIX}{corpus.name}:{_fingerprint(corpus, ingest)}"
        existing = await self._find(name)
        if existing is not None:
            docs = await self._documents(existing["id"])
            names = {d["name"] for d in docs}
            ok = all(d["indexing_status"] in (*_RUNNING, "completed") for d in docs)
            if ok and names == {d.doc_id for d in corpus.documents}:
                return KbHandle(self.name, existing["id"], name, reused=True)
            # 不完整（上次中断或有失败）：删掉重建，保证可复现
            await self._req("DELETE", f"/v1/datasets/{existing['id']}")

        doc_form = ingest.get("doc_form", "hierarchical_model")
        body: dict[str, Any] = {
            "name": name,
            "description": (
                f"评测语料 {corpus.name} v{corpus.version}（atlas-bench 自动创建，可删除）"
            ),
            "permission": "only_me",
            "doc_form": doc_form,
        }
        if ingest.get("embedding_model"):
            body["embedding_model"] = ingest["embedding_model"]
        if ingest.get("enhancements"):  # 入库增强：上下文补全 L2、表格摘要
            body["enhancements"] = ingest["enhancements"]
        ds = await self._req("POST", "/v1/datasets", json=body)

        process_rule = ingest.get("process_rule")
        if isinstance(process_rule, str):
            process_rule = {"mode": process_rule}
        docs = list(corpus.documents)
        for i in range(0, len(docs), _UPLOAD_BATCH):
            files = []
            for d in docs[i : i + _UPLOAD_BATCH]:
                mime = mimetypes.guess_type(d.doc_id)[0] or "application/octet-stream"
                files.append(("files", (d.doc_id, Path(d.path).read_bytes(), mime)))
            data = {"doc_form": doc_form}
            if process_rule:
                data["process_rule"] = json.dumps(process_rule, ensure_ascii=False)
            await self._req("POST", f"/v1/datasets/{ds['id']}/documents", files=files, data=data)
        return KbHandle(self.name, ds["id"], name)

    async def wait_ready(self, kb: KbHandle, *, timeout_s: float) -> IngestReport:
        started = time.monotonic()
        while True:
            docs = await self._documents(kb.kb_id)
            if not any(d["indexing_status"] in _RUNNING for d in docs):
                failed = [
                    f"{d['name']}：{d['error']}"
                    for d in docs
                    if d["indexing_status"] != "completed"
                ]
                return IngestReport(len(docs), failed, round(time.monotonic() - started, 1))
            if time.monotonic() - started > timeout_s:
                running = sum(1 for d in docs if d["indexing_status"] in _RUNNING)
                raise AtlasError(f"等待入库超过 {timeout_s:.0f} 秒，仍有 {running} 份文档在入库")
            await asyncio.sleep(2)

    # ------------------------------------------------------------------ 检索

    async def retrieve(
        self, kb: KbHandle, question: str, *, top_k: int, retrieval: dict[str, Any]
    ) -> list[Chunk]:
        res = await self._req(
            "POST",
            f"/v1/datasets/{kb.kb_id}/hit-testing",
            json={"query": question, "retrieval_config": {**retrieval, "top_k": top_k}},
        )
        return [
            Chunk(
                text=r["content"],
                score=r.get("score"),
                doc_name=r.get("document_name"),
                title_path=list(r.get("title_path") or []),
                child_texts=[c["content"] for c in r.get("child_chunks") or []],
            )
            for r in res["records"]
        ]

    async def cleanup(self, kb: KbHandle) -> None:
        await self._req("DELETE", f"/v1/datasets/{kb.kb_id}")

    async def cleanup_all(self) -> list[str]:
        """删除评测建的全部库（库名前缀 bench:）。"""
        removed = []
        page = await self._req("GET", "/v1/datasets", params={"keyword": KB_PREFIX, "limit": 100})
        for d in page["data"]:
            if d["name"].startswith(KB_PREFIX):
                await self._req("DELETE", f"/v1/datasets/{d['id']}")
                removed.append(d["name"])
        return removed
