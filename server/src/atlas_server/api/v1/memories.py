"""记忆管理（记忆设计 §10）。

自动抽取意味着用户从没主动说过「请记住这个」，系统却替他记下了。这条
产品性质决定了管理界面**不是可选项**：凡是会影响后续回答的记忆，用户都
应当能查到、删掉，且不需要联系任何人。做不到这一点，记忆就变成了一个
用户无法控制的黑盒 —— 出错时他唯一的办法是反复纠正模型，而每次纠正又
可能被抽取成新的记忆。

本轮只做**浏览**与**删除单条**：
  · 浏览是观察提炼质量的唯一手段，而这正是本轮的目的（§12 第 1 项）
  · 删除是发现记错时的唯一出口
更正 / 批删 / 导出 / 暂停开关在 §10 里，随后续轮次补。

★ 这些端点**绝不能做成工具挂给任何 Agent**（§10）。管理操作是用户的权利，
  不是 Agent 的能力：对顾问它会随 regenerate 重复执行，对助理它意味着
  模型在处理不可信文本时可能被诱导删除用户数据。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ...deps import CurrentUserDep
from ...schemas.memory import MemoryListOut, to_out

router = APIRouter(prefix="/memories", tags=["memories"])


def _memory(request: Request):
    """取进程级的记忆客户端；没开记忆就 503。

    ★ 明确报 503，不返回空列表。空列表会让「没开记忆」与「还没记下任何
      东西」看起来一模一样 —— 而这两者的处置完全不同。
    """
    memory = getattr(request.app.state, "memory", None)
    if memory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="记忆功能未启用（MEMORY_ENABLED=false）",
        )
    return memory


MemoryDep = Annotated[object, Depends(_memory)]


@router.get("", response_model=MemoryListOut)
async def list_memories(
    memory: MemoryDep,
    user_id: CurrentUserDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    workspace: str | None = None,
    source_session: str | None = None,
) -> MemoryListOut:
    """列出**当前用户**的记忆。

    ★ user_id 由服务端从会话取，**不接受参数传入**（§09）。Mem0 的
      filters 决定了能读到谁的记忆 —— 一旦它可以由调用方指定，就等于
      开了一个读取他人记忆的口子。
    """
    extra: dict[str, str] = {}
    if workspace:
        extra["workspace"] = workspace
    if source_session:
        extra["source_session"] = source_session

    rows = await memory.get_all(user_id=user_id, limit=limit, **extra)  # type: ignore[attr-defined]
    return MemoryListOut(data=[to_out(r) for r in rows])


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memory(
    memory_id: str, memory: MemoryDep, user_id: CurrentUserDep
) -> None:
    """删除单条。

    ★ 先确认这条属于当前用户再删 —— Mem0 的 delete(memory_id) 不带作用域，
      直接透传等于任何人都能删别人的记忆。

    ★ 措辞上**不承诺「彻底删除」**：delete() 到底是真把向量与原文清掉、
      还是只标记为不可见，属于 §12 第 7 项的待验证项。在验证之前对用户
      说「已彻底删除」是不诚实的 —— 而用户删记忆往往正是因为它敏感。
    """
    rows = await memory.get_all(user_id=user_id, limit=500)  # type: ignore[attr-defined]
    if not any(str(r.get("id")) == memory_id for r in rows):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="记忆不存在")
    await memory.delete(memory_id)  # type: ignore[attr-defined]
