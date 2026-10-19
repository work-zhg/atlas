"""/mcp：给 Pod 里 ACP CLI 用的 MCP 端点（streamable HTTP，无状态），工具 knowledge_search。

鉴权：请求头 X-Atlas-Knowledge-Token，由 atlas-server 在打开 ACP 会话时签发：
    base64url({"d": [库 id], "t": 会话 id, "u": auth-uid, "o": 发起人})
    + "." + HMAC-SHA256(mcp_secret)
能查哪些库只看令牌里的 d —— 与原生工具一样，库 id 不进工具参数。文档级权限按 u（按谁判），
检索记录里记 o（谁在用；旧令牌没有 o 时取 u）。

★ 会话 Pod 跑的是不可信代码：这里只认令牌，不认任何身份请求头（内网身份传递 §5.3）。

★ 关掉 FastMCP 的 DNS rebinding 防护：它默认只放行 localhost 的 Host 头，而 Pod 用集群内
  Service 域名访问。这里的防线是令牌，不是 Host 头。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from typing import Any
from uuid import UUID

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from ..auth import use_identity
from ..db.session import get_sessionmaker
from ..errors import KnowledgeError
from ..runtime import Runtime
from ..services.retrieval import KnowledgeRetrieval

__all__ = ["build_mcp", "verify_token"]

logger = logging.getLogger(__name__)

TOKEN_HEADER = "x-atlas-knowledge-token"


class TokenInvalid(Exception):
    pass


def verify_token(secret: str, token: str) -> dict[str, Any]:
    body, _, sig = token.partition(".")
    if not body or not sig or not secret:
        raise TokenInvalid("令牌格式错误")
    expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        raise TokenInvalid("令牌签名无效")
    padded = body + "=" * (-len(body) % 4)
    data = json.loads(base64.urlsafe_b64decode(padded))
    if not isinstance(data, dict) or not isinstance(data.get("d"), list) or not data["d"]:
        raise TokenInvalid("令牌内容无效")
    return data


def _format(query: str, records: list[dict[str, Any]]) -> str:
    if not records:
        return f"知识库里没有找到与「{query}」相关的内容。"
    parts = []
    for i, r in enumerate(records, 1):
        name = r.get("document_name") or "未命名文档"
        where = " > ".join(r.get("title_path") or [])
        title = (
            f"[{i}] 《{name}》{' ' + where if where else ''}（知识库：{r.get('dataset_name', '')}）"
        )
        parts.append(f"{title}\n{str(r.get('content') or '').strip()}")
    return f"与「{query}」相关的知识库内容（{len(records)} 条，按相关度排序）：\n\n" + "\n\n".join(
        parts
    )


def build_mcp(runtime_of: Any) -> FastMCP:
    """runtime_of：() -> Runtime（应用启动后才有 Runtime，所以传取值函数）。"""
    mcp = FastMCP(
        "atlas-knowledge",
        instructions="Atlas 知识库：为这个智能体配置的领域知识（文档、制度、FAQ）。",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @mcp.tool(
        name="knowledge_search",
        description=(
            "在为你配置的知识库里检索资料：产品文档、制度规范、FAQ 等领域知识。"
            "回答涉及这些领域的问题前先检索，并在回答里注明出处。只读。"
        ),
    )
    async def knowledge_search(query: str, ctx: Context) -> str:  # type: ignore[type-arg]
        rt: Runtime = runtime_of()
        request = ctx.request_context.request
        token = request.headers.get(TOKEN_HEADER, "") if request is not None else ""
        secret = rt.settings.mcp_secret.get_secret_value() if rt.settings.mcp_secret else ""
        try:
            claims = verify_token(secret, token)
        except (TokenInvalid, ValueError) as exc:
            logger.warning("知识库 MCP 令牌无效：%s", exc)
            return "知识库访问凭据无效，无法检索。"
        # 主体展开（atlas-server → 用户中心）带令牌里的身份，而不是 Pod 发来的请求头
        use_identity(claims.get("o") or claims.get("u"), claims.get("u"))
        async with get_sessionmaker()() as session:
            try:
                res = await KnowledgeRetrieval(session, rt).retrieve(
                    None,
                    [UUID(d) for d in claims["d"]],
                    query,
                    {"top_k": 4},
                    source="mcp",
                    source_ref=f"thread:{claims.get('t')}",
                    actor=claims.get("o") or claims.get("u"),
                    acl_subject=claims.get("u"),
                )
                await session.commit()
            except KnowledgeError as exc:
                return f"检索失败：{exc.message}"
        return _format(query, [r.as_dict() for r in res.records])

    return mcp
