"""检索接口（c4-model/knowledge/component-api.html · 检索接口）：/v1 下和检索有关的全部。

    POST /v1/retrieve                          检索：按库（Atlas、控制台），或按知识集和库的名称
                                               （MCP 网关替 CLI 智能体调）
    POST /v1/knowledge-sets/readable           能检索哪些知识集，每个带上其中能读的库
    POST /v1/datasets/readable                 能读哪些库（名片）
    POST /v1/datasets/{id}/hit-testing         召回测试（单库，可覆盖检索设置、指定索引版本）
    GET  /v1/datasets/{id}/queries             这个库的检索记录
    GET  /v1/retrievals/{id}                   一次检索的漏斗

★ 接口按功能分，不按调用方分：浏览器经网关、Atlas、MCP 网关调的是同一套接口，身份一律取请求头
  （X-Uid 谁在用，X-Auth-Uid 按谁判，缺 X-Uid 一律 401，没有别的兜底）。本服务不判断请求从哪来。
★ 库级权限按 X-Auth-Uid：读不到的库进 skipped（只给 id，不暴露库是否存在），调用方提示
  「另有 N 个库没有访问权限」；一个都读不到就返回空结果。
★ 没有文档级权限：能读库就能检索库里全部文档。
★ 库 id 不进工具参数：CLI 智能体经 MCP 网关调时只给知识集和库的名称（来自提示词里的目录和列库
  工具），在 auth-uid 能用的知识集里核对后换成库。代价：CLI 能查的是「auth-uid 能用的知识集」，
  不再钉死在智能体选的那几个。
★ 只返回数据，不为调用方渲染文字：给 CLI 看哪些字段由 MCP 网关的工具映射挑。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..access.auth import verified_tenant
from ..access.permission import visible_clause
from ..dataset.cards import dataset_cards
from ..dataset.datasets import DatasetService
from ..dataset.sets import KnowledgeSetService
from ..db.models import Dataset
from ..errors import Invalid
from ..index.index_versions import IndexVersionService
from ..retrieval.service import KnowledgeRetrieval, RetrieveOptions
from .deps import PrincipalDep, RuntimeDep, SessionDep
from .serializers import query_out

__all__ = ["base_labels", "router"]

router = APIRouter(prefix="/v1", dependencies=[Depends(verified_tenant)])

#: 按名称检索时最多指定几个库（不指定就查整个知识集）
MAX_PICK = 5

Page = Annotated[int, Query(ge=1)]
Limit = Annotated[int, Query(ge=1, le=100)]


def base_labels(cards: list[dict[str, Any]]) -> dict[str, str]:
    """库在工具里的名称 → 库 id。同一个知识集里重名的，名称后加 id 前 6 位区分。

    ★ 规则与 Atlas 原生工具的一致（atlas_agent/providers/knowledge.py::base_labels）：两种智能体
      看到的库名一样。
    """
    counts: dict[str, int] = {}
    for c in cards:
        counts[c["name"]] = counts.get(c["name"], 0) + 1
    return {
        (c["name"] if counts[c["name"]] == 1 else f"{c['name']}（{c['id'][:6]}）"): c["id"]
        for c in cards
    }


class ScopeBody(BaseModel):
    """检索的企业级参数（§7）：对话历史（改写用）、元数据过滤、按时间点、含历史版本。"""

    history: list[dict[str, str]] | None = Field(default=None, max_length=50)
    filter: dict[str, Any] | None = None
    as_of: datetime | None = None
    include_superseded: bool = False

    def options(self) -> RetrieveOptions:
        return RetrieveOptions(
            history=self.history,
            filter=self.filter,
            as_of=self.as_of,
            include_superseded=self.include_superseded,
        )


class RetrieveBody(ScopeBody):
    """按库（dataset_ids）或按名称（knowledge_set、knowledge_bases），二选一。"""

    #: ★ 「使用者能读的全部」这样的知识集可能有上百个库：超过路由阈值时先按库的介绍挑库再查
    dataset_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=200)
    #: 知识集名称。只有一个能用的知识集时可以空着
    knowledge_set: str | None = None
    #: 只查这几个库（名称来自列库的结果）；不给就查整个知识集
    knowledge_bases: list[str] | None = Field(default=None, max_length=MAX_PICK)
    query: str
    retrieval_config: dict[str, Any] | None = None
    #: 检索记录里的来源：api（控制台）| agent（原生智能体）| mcp（CLI 智能体，经 MCP 网关）
    source: Literal["api", "agent", "mcp"] = "api"
    #: 会话 / run 引用，进检索记录；不给时取请求头 X-Thread-Id
    source_ref: str | None = None


async def _by_name(
    svc: KnowledgeSetService, p: Any, body: RetrieveBody
) -> tuple[list[UUID], dict[str, Any]]:
    """知识集和库的名称 → (库, 检索设置)。在 auth-uid 能用的知识集里核对；对不上的说清楚可选什么
    （MCP 网关把这句话原样交给模型，模型改了再调）。"""
    sets = await svc.readable(p, None)
    if not sets:
        raise Invalid("没有能检索的知识集")
    names = "、".join(s["name"] for s in sets)
    wanted = (body.knowledge_set or "").strip()
    if not wanted:
        if len(sets) != 1:
            raise Invalid(f"请指定知识集（knowledge_set），可选：{names}")
        picked = sets[0]
    else:
        picked = next((s for s in sets if s["name"] == wanted), None)
        if picked is None:
            raise Invalid(f"没有知识集「{wanted}」，可选：{names}")
    named = base_labels(picked["datasets"])
    ids = list(named.values())
    if body.knowledge_bases:
        unknown = [b for b in body.knowledge_bases if b not in named]
        if unknown:
            raise Invalid(
                f"知识集「{picked['name']}」里没有这些知识库：{'、'.join(unknown)}。"
                "名称要和列库结果里的一致"
            )
        ids = [named[b] for b in dict.fromkeys(body.knowledge_bases)]
    # 取几条用知识集的（维护人定的），门槛用各库自己的；调用方显式给了就按调用方的
    cfg = {"top_k": picked["top_k"], **(body.retrieval_config or {})}
    return [UUID(i) for i in ids], cfg


@router.post("/retrieve")
async def retrieve(
    body: RetrieveBody, request: Request, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> dict[str, Any]:
    if body.dataset_ids is not None:
        if body.knowledge_set is not None or body.knowledge_bases:
            raise Invalid("按库（dataset_ids）和按名称（knowledge_set）只能二选一")
        ids, cfg = list(body.dataset_ids), body.retrieval_config
    else:
        ids, cfg = await _by_name(KnowledgeSetService(session, rt), p, body)
    thread = (request.headers.get("x-thread-id") or "").strip()
    res = await KnowledgeRetrieval(session, rt).retrieve(
        None,
        ids,
        body.query,
        cfg,
        source=body.source,
        source_ref=body.source_ref or (f"thread:{thread}" if thread else None),
        actor=p.uid,
        auth_uid=p.auth_uid,
        opts=body.options(),
    )
    return res.as_dict(body.query)


class ReadableSetsBody(BaseModel):
    #: None = 使用者能读的全部
    set_ids: list[UUID] | None = Field(default=None, max_length=50)
    #: 只要这一个知识集（按名称；MCP 网关的列库工具用）
    name: str | None = None


@router.post("/knowledge-sets/readable")
async def readable_sets(
    body: ReadableSetsBody, p: PrincipalDep, session: SessionDep, rt: RuntimeDep
) -> dict[str, Any]:
    """auth-uid 能用的知识集（一个能读的库都没有的不返回），每个带上其中能读的库的名片：
    Atlas 编辑智能体时列可选的、运行时展开智能体选的；MCP 网关的列库工具。"""
    sets = await KnowledgeSetService(session, rt).readable(p, body.set_ids)
    wanted = (body.name or "").strip()
    if wanted:
        sets = [s for s in sets if s["name"] == wanted]
    return {"data": sets}


class ReadableBody(BaseModel):
    dataset_ids: list[UUID] | None = None


@router.post("/datasets/readable")
async def readable(
    body: ReadableBody, p: PrincipalDep, session: SessionDep, rt: RuntimeDep
) -> dict[str, Any]:
    """auth-uid 能读的库（≥ READ）：名称、介绍、文档数、最近入库时间。"""
    q = select(Dataset).where(await visible_clause(rt.perms, p))
    if body.dataset_ids is not None:
        q = q.where(Dataset.uuid.in_(body.dataset_ids))
    rows = (await session.scalars(q.order_by(Dataset.name))).all()
    return {"data": await dataset_cards(session, rows)}


# ───────────────────────────── 召回测试、检索记录与漏斗


class HitTestingBody(ScopeBody):
    query: str
    retrieval_config: dict[str, Any] | None = None
    #: 用指定的索引版本检索（切换前对比新旧版本，§6.2）；默认 active 版本
    index_version_id: UUID | None = None


@router.post("/datasets/{dataset_id}/hit-testing")
async def hit_testing(
    dataset_id: UUID, body: HitTestingBody, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> dict[str, Any]:
    access = await DatasetService(session, rt).access(dataset_id, p)
    version = None
    if body.index_version_id is not None:
        version = await IndexVersionService(session, rt).get(access.ds, body.index_version_id)
    return await KnowledgeRetrieval(session, rt).hit_testing(
        access, body.query, body.retrieval_config, version=version, opts=body.options()
    )


@router.get("/datasets/{dataset_id}/queries")
async def list_queries(
    dataset_id: UUID,
    session: SessionDep,
    rt: RuntimeDep,
    p: PrincipalDep,
    page: Page = 1,
    limit: Limit = 20,
) -> dict[str, Any]:
    access = await DatasetService(session, rt).access(dataset_id, p)
    rows = await KnowledgeRetrieval(session, rt).queries(access, page=page, limit=limit)
    return {"data": [query_out(q) for q in rows], "page": page, "limit": limit}


@router.get("/retrievals/{retrieval_id}")
async def get_retrieval(
    retrieval_id: UUID, session: SessionDep, rt: RuntimeDep, p: PrincipalDep
) -> dict[str, Any]:
    """一次检索的过程：各环节候选的漏斗（排查「为什么没检索到 / 为什么排在后面」）。"""
    svc = KnowledgeRetrieval(session, rt)
    return await svc.funnel(await svc.get(p, retrieval_id))
