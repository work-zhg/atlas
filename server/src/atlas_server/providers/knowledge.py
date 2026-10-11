"""接入知识库服务（atlas-knowledge，doc/atlas-rag-design.html §10）。

    KnowledgeClient        调知识库的 /internal/v1（服务间令牌）
    make_knowledge_tool()  原生 Agent 的 knowledge_search 工具（tool 模式）
    format_context()       auto 模式：检索结果渲染成并入本轮输入的参考资料
    mcp_server_entry()     acp 会话的 MCP 下发项（Pod 里的 CLI 经它调 knowledge_search）

★ 检索范围钉死在智能体绑定的知识库上，不进工具参数 —— 与记忆工具同一条纪律：
  一旦库 id 可由模型指定，提示词注入就能读到未绑定的库。
★ MCP 令牌：HMAC(服务间令牌, {库 id, 会话, 发起人})。不设过期 —— ACP 会话只在 open 时
  下发一次 MCP，会话可能挂起很久再恢复（Atlas 不设到点结束的超时）；令牌只授予「读这几个
  已绑定的库」，换绑后新会话拿新令牌。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from typing import Any
from uuid import UUID

import httpx
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from ..domain.spec import KnowledgeSpec
from ..errors import CapabilityUnavailable

__all__ = [
    "KNOWLEDGE_SEARCH_TOOL",
    "KnowledgeClient",
    "KnowledgeUnavailable",
    "format_context",
    "make_knowledge_client",
    "make_knowledge_tool",
    "mcp_server_entry",
    "sign_mcp_token",
]

logger = logging.getLogger(__name__)

KNOWLEDGE_SEARCH_TOOL = "knowledge_search"
MCP_SERVER_NAME = "atlas-knowledge"


class KnowledgeUnavailable(CapabilityUnavailable):
    """绑定了知识库，但知识库服务未接入或不可用 —— 明确失败，不静默少装能力（§13.2）。"""


class KnowledgeClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout_s
        self._transport = transport

    @property
    def token(self) -> str:
        return self._token

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, trust_env=False, transport=self._transport
            ) as client:
                r = await client.post(
                    f"{self.base_url}{path}", json=body, headers={"X-Internal-Token": self._token}
                )
        except httpx.HTTPError as exc:
            raise KnowledgeUnavailable(f"知识库服务不可达：{exc}") from exc
        if r.status_code >= 400:
            detail = ""
            try:
                detail = str(r.json().get("detail") or "")
            except ValueError:
                detail = r.text[:200]
            raise KnowledgeUnavailable(f"知识库服务返回 {r.status_code}：{detail}")
        return r.json()  # type: ignore[no-any-return]

    async def retrieve(
        self,
        spec: KnowledgeSpec,
        query: str,
        *,
        source: str,
        source_ref: str | None,
        actor: UUID | None,
    ) -> list[dict[str, Any]]:
        body = await self._post(
            "/internal/v1/retrieve",
            {
                "dataset_ids": list(spec.dataset_ids),
                "query": query,
                "retrieval_config": spec.retrieval_config(),
                "source": source,
                "source_ref": source_ref,
                "actor": str(actor) if actor else None,
            },
        )
        return list(body.get("records") or [])

    async def readable(
        self, principal_id: UUID, operations: frozenset[str], dataset_ids: list[str] | None = None
    ) -> list[dict[str, Any]]:
        body = await self._post(
            "/internal/v1/datasets/readable",
            {
                "principal_id": str(principal_id),
                "operations": sorted(operations),
                "dataset_ids": dataset_ids,
            },
        )
        return list(body.get("data") or [])


def make_knowledge_client(settings: Any) -> KnowledgeClient | None:
    url = settings.knowledge_base_url
    if not url:
        return None
    token = (
        settings.config_internal_token.get_secret_value() if settings.config_internal_token else ""
    )
    return KnowledgeClient(url, token, timeout_s=settings.knowledge_timeout_s)


def require_client(settings: Any) -> KnowledgeClient:
    client = make_knowledge_client(settings)
    if client is None:
        raise KnowledgeUnavailable(
            "智能体绑定了知识库，但服务端未接入知识库服务（KNOWLEDGE_BASE_URL）"
        )
    return client


# ───────────────────────────────────────────── 渲染


def _record_text(i: int, rec: dict[str, Any]) -> str:
    name = rec.get("document_name") or "未命名文档"
    title = f"[{i}] 《{name}》（知识库：{rec.get('dataset_name', '')}）"
    return f"{title}\n{str(rec.get('content') or '').strip()}"


def format_records(query: str, records: list[dict[str, Any]]) -> str:
    """工具结果。★ 空结果要明说，否则模型会把「没查到」当成工具坏了。"""
    if not records:
        return f"知识库里没有找到与「{query}」相关的内容。"
    body = "\n\n".join(_record_text(i, r) for i, r in enumerate(records, 1))
    return f"与「{query}」相关的知识库内容（{len(records)} 条，按相关度排序）：\n\n{body}"


def format_context(question: Any, records: list[dict[str, Any]]) -> Any:
    """auto 模式：把参考资料并入本轮输入（只影响这一轮模型看到的内容，不落库）。

    question 可能是纯文本，也可能是带附件的内容块列表 —— 后者在最前面插一个文本块。
    """
    if not records:
        return question
    refs = "\n\n".join(_record_text(i, r) for i, r in enumerate(_reorder(records), 1))
    block = (
        "以下是从知识库检索到的参考资料。回答时优先依据这些资料，并用 [编号] 标注出处；"
        "资料与问题无关或不足以回答时，如实说明，不要编造。\n"
        f"<knowledge>\n{refs}\n</knowledge>\n\n用户的问题："
    )
    if isinstance(question, str):
        return f"{block}\n{question}"
    if isinstance(question, list):
        return [{"type": "text", "text": block}, *question]
    return question


def _reorder(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """高分放首尾（同 Dify ReorderRunner，缓解长上下文中间被忽略）。"""
    return items[::2] + items[1::2][::-1]


# ───────────────────────────────────────────── 原生工具


class _Args(BaseModel):
    query: str = Field(description="要在知识库里查找的内容，用自然语言描述，越具体越好。")


def make_knowledge_tool(
    client: KnowledgeClient,
    spec: KnowledgeSpec,
    *,
    names: list[str] | None,
    source_ref: str,
    actor: UUID | None,
) -> StructuredTool:
    """库 id 走闭包捕获，不进参数（见模块开头）。"""

    async def knowledge_search(query: str) -> str:
        try:
            records = await client.retrieve(
                spec, query, source="agent", source_ref=source_ref, actor=actor
            )
        except KnowledgeUnavailable as exc:
            logger.warning("知识库检索失败：%s", exc)
            return f"知识库暂时不可用（{exc}），这次回答无法引用知识库。"
        return format_records(query, records)

    scope = f"（可查的知识库：{'、'.join(names)}）" if names else ""
    return StructuredTool.from_function(
        coroutine=knowledge_search,
        name=KNOWLEDGE_SEARCH_TOOL,
        description=(
            "在为你配置的知识库里检索资料：产品文档、制度规范、FAQ 等领域知识。"
            "回答涉及这些领域的问题前先检索，并在回答里注明出处。"
            f"只读。{scope}"
        ),
        args_schema=_Args,
    )


# ───────────────────────────────────────────── acp：MCP 下发


def sign_mcp_token(
    secret: str, *, dataset_ids: list[str], thread_id: UUID, actor: UUID | None
) -> str:
    payload = json.dumps(
        {"d": dataset_ids, "t": str(thread_id), "u": str(actor) if actor else None},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def mcp_server_entry(
    client: KnowledgeClient, spec: KnowledgeSpec, *, thread_id: UUID, actor: UUID | None
) -> dict[str, Any]:
    token = sign_mcp_token(
        client.token, dataset_ids=list(spec.dataset_ids), thread_id=thread_id, actor=actor
    )
    return {
        "type": "http",
        "name": MCP_SERVER_NAME,
        "url": f"{client.base_url}/mcp/",
        # ★ ACP 的 headers 是数组，不是对象
        "headers": [{"name": "X-Atlas-Knowledge-Token", "value": token}],
    }
