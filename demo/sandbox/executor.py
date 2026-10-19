"""执行器（server 侧）：ServerInstance 就是一个 server 进程，负责跑一轮对话。

    run_turn   读会话历史 → 建图（装上这个 run 的 PodSandbox）→ 跑 → 新消息落库

★ 每个 run 新建一个 PodSandbox，同一工作区的多个实例指向同一个 Pod。
★ 进程内存里只有 boots（每个工作区最近见到的 sandboxd boot_id）；会话消息在数据库里，所有实例共用。
★ crash() 模拟进程崩溃（真实系统里没有这个）：见 InstanceCrashed。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import httpx
from agent import build_agent
from cluster import PodManager
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from sandbox_client import PodSandbox
from storage import ObjectStorage
from tracing import enter, say


@dataclass
class Conversation:
    """会话（数据库，内存模拟）：消息表 + run 表。所有 server 实例共用。"""

    thread_id: str
    user_id: str
    messages: list[BaseMessage] = field(default_factory=list)
    runs: dict[str, str] = field(default_factory=dict)  # run_id → 状态


class InstanceCrashed(BaseException):
    """模拟 server 进程崩溃：从这一刻起它的代码一行都不再执行。

    继承 BaseException 而不是 Exception：PodSandbox 的重连、中间件的错误转换都接不住它，
    取消路径（发 kill）也不会走；只有 async with 的收尾会跑 —— 相当于操作系统替死掉的进程关掉连接。
    """


class InstanceNetwork(httpx.AsyncBaseTransport):
    """一个 server 进程发出的全部 HTTP 请求。crash() 之后，正在读的流在下一个数据块处中断。"""

    def __init__(self) -> None:
        self._inner = httpx.AsyncHTTPTransport()
        self.crashed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.crashed:
            raise InstanceCrashed
        response = await self._inner.handle_async_request(request)
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=_Watched(response.stream, self),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        pass  # 每个请求一个 AsyncClient，退出时会来关 transport；进程的网络栈不跟着关


class _Watched(httpx.AsyncByteStream):
    def __init__(self, inner: httpx.AsyncByteStream, net: InstanceNetwork) -> None:
        self._inner, self._net = inner, net

    async def __aiter__(self):
        async for chunk in self._inner:
            if self._net.crashed:
                raise InstanceCrashed
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


class ServerInstance:
    def __init__(
        self, name: str, cluster: PodManager, storage: ObjectStorage, db: Conversation
    ) -> None:
        self.name = name
        self.cluster = cluster
        self.storage = storage
        self.db = db
        self.boots: dict[str, str] = {}  # 进程内存：Pod 重建了没有，靠它发现
        self.network = InstanceNetwork()

    def crash(self) -> None:
        self.network.crashed = True

    async def run_turn(self, run_id: str) -> None:
        enter(run_id)
        db = self.db
        sandbox = PodSandbox(
            cluster=self.cluster,
            workspace_thread_id=db.thread_id,  # 子智能体传的也是父会话的工作区：共用一个 Pod
            user_id=db.user_id,
            run_id=run_id,
            boots=self.boots,
            transport=self.network,
        )
        agent = build_agent(sandbox, self.storage, f"{db.user_id}/{db.thread_id}/workspace/")
        db.runs[run_id] = f"running（实例 {self.name}）"
        latest = list(db.messages)
        try:
            async for state in agent.astream({"messages": latest}, stream_mode="values"):
                latest = state["messages"]
        except asyncio.CancelledError:
            # 取消：已经做完的落库，悬空的调用补一条「已取消」，下一轮的历史才是完整的
            db.messages = _patch_dangling(latest)
            db.runs[run_id] = "cancelled"
            raise
        db.messages = latest
        db.runs[run_id] = f"succeeded（实例 {self.name}）"
        say("执行器", f"{run_id} 结束，消息落库")


def _patch_dangling(messages: list[BaseMessage]) -> list[BaseMessage]:
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    patched = list(messages)
    for m in messages:
        if isinstance(m, AIMessage):
            for call in m.tool_calls:
                if call["id"] not in answered:
                    patched.append(ToolMessage("用户取消了这一轮", tool_call_id=call["id"]))
    return patched
