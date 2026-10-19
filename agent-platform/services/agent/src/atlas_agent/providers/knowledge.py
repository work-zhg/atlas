"""接入知识库服务（atlas-knowledge，doc/atlas-rag-design.html §10）。

    KnowledgeClient        调知识库的 /internal/v1（内网免令牌：带本次调用的身份请求头）
    make_knowledge_tool()  原生 Agent 的 knowledge_search 工具（tool 模式）
    format_context()       auto 模式：检索结果渲染成并入本轮输入的参考资料
    mcp_server_entry()     acp 会话的 MCP 下发项（Pod 里的 CLI 经它调 knowledge_search）

★ 检索范围钉死在智能体绑定的知识库上，不进工具参数 —— 与记忆工具同一条纪律：
  一旦库 id 可由模型指定，提示词注入就能读到未绑定的库。
★ 检索按谁判：请求头 X-Auth-Uid（run 执行时 = 这一轮的 auth-uid，内网身份传递 §8）。
★ MCP 令牌：HMAC(knowledge_mcp_secret, {库 id, 会话, u: auth-uid, o: 发起人})。这是给会话 Pod
  （不可信代码）的能力凭据，内网去令牌后仍保留（§5.3）。不设过期 —— ACP 会话只在 open 时
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
from opentelemetry import propagate
from opentelemetry.trace import SpanKind
from pydantic import BaseModel, Field

from ..domain.spec import KnowledgeSpec
from ..errors import CapabilityUnavailable
from ..outbound import current_identity, outbound_headers
from ..telemetry import tracer

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
        mcp_secret: str = "",
        *,
        timeout_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._mcp_secret = mcp_secret
        self._timeout = timeout_s
        self._transport = transport

    @property
    def mcp_secret(self) -> str:
        return self._mcp_secret

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        headers = outbound_headers()
        # ★ 带上 traceparent：知识库那边的检索 span（召回、重排、阈值…）挂到这次 run 的链路下
        propagate.inject(headers)
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, trust_env=False, transport=self._transport
            ) as client:
                r = await client.post(f"{self.base_url}{path}", json=body, headers=headers)
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
    ) -> list[dict[str, Any]]:
        with tracer().start_as_current_span(
            "atlas.knowledge.retrieve", kind=SpanKind.CLIENT
        ) as span:
            span.set_attribute("atlas.knowledge.source", source)
            span.set_attribute("atlas.knowledge.dataset_ids", [str(d) for d in spec.dataset_ids])
            body = await self._post(
                "/internal/v1/retrieve",
                {
                    "dataset_ids": list(spec.dataset_ids),
                    "query": query,
                    "retrieval_config": spec.retrieval_config(),
                    "source": source,
                    "source_ref": source_ref,
                },
            )
            records = list(body.get("records") or [])
            span.set_attribute("atlas.knowledge.results", len(records))
            # 知识库的检索过程记录：拿它去「召回测试 → 最近检索」看漏斗
            if rid := body.get("retrieval_id"):
                span.set_attribute("atlas.knowledge.retrieval_id", str(rid))
        return records

    async def readable(self, dataset_ids: list[str] | None = None) -> list[dict[str, Any]]:
        """本次调用的 auth-uid 能挂哪些库（用户中心数据权限 AtlasKnowledgeBase ≥ READ）。"""
        body = await self._post("/internal/v1/datasets/readable", {"dataset_ids": dataset_ids})
        return list(body.get("data") or [])


def make_knowledge_client(settings: Any) -> KnowledgeClient | None:
    url = settings.knowledge_base_url
    if not url:
        return None
    secret = (
        settings.knowledge_mcp_secret.get_secret_value() if settings.knowledge_mcp_secret else ""
    )
    return KnowledgeClient(url, secret, timeout_s=settings.knowledge_timeout_s)


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
    # 按标题分段的结果带标题路径：模型据此知道这段在文档里的位置
    where = " > ".join(rec.get("title_path") or [])
    title = (
        f"[{i}] 《{name}》{' ' + where if where else ''}（知识库：{rec.get('dataset_name', '')}）"
    )
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
) -> StructuredTool:
    """库 id 走闭包捕获，不进参数（见模块开头）。按谁判看本次 run 的出站身份。"""

    async def knowledge_search(query: str) -> str:
        try:
            records = await client.retrieve(spec, query, source="agent", source_ref=source_ref)
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
    secret: str,
    *,
    dataset_ids: list[str],
    thread_id: UUID,
    auth_uid: UUID | None,
    uid: UUID | None = None,
) -> str:
    """u = 检索按谁判（auth-uid）；o = 发起人（只进检索记录）。内网身份传递 §5.3。"""
    payload = json.dumps(
        {
            "d": dataset_ids,
            "t": str(thread_id),
            "u": str(auth_uid) if auth_uid else None,
            "o": str(uid) if uid else None,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def mcp_server_entry(
    client: KnowledgeClient, spec: KnowledgeSpec, *, thread_id: UUID
) -> dict[str, Any]:
    """身份取本次 run 的出站身份（执行器在 run 开始时设好）。"""
    ident = current_identity()
    token = sign_mcp_token(
        client.mcp_secret,
        dataset_ids=list(spec.dataset_ids),
        thread_id=thread_id,
        auth_uid=ident.auth_uid if ident else None,
        uid=ident.uid if ident else None,
    )
    return {
        "type": "http",
        "name": MCP_SERVER_NAME,
        "url": f"{client.base_url}/mcp/",
        # ★ ACP 的 headers 是数组，不是对象
        "headers": [{"name": "X-Atlas-Knowledge-Token", "value": token}],
    }
