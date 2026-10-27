"""执行器（server 侧）：ServerInstance 就是一个 server 进程（Atlas 可以多副本）。

    run_turn   读会话历史 → 建图（装上这个 run 的 PodSandbox 与 WorkspaceFilesystem）
               → 跑 → 新消息落库

★ 进程之间共享的只有 Redis（路由表、端点缓存、令牌、锁）、数据库和对象存储；
  命令映射、「上次见到的沙箱」在各自的进程内存里（ProcessMemory）。
  r3 换实例 B 来跑：它没 ensure 过，但从 Redis 的路由表就知道该把文件工具送进沙箱。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
from agent import build_agent
from langchain_core.messages import BaseMessage
from provision import Ensurer
from sandbox_client import PodSandbox, ProcessMemory
from storage import ObjectStorage
from tracing import enter, say
from workspace import OssStore, WorkspaceFilesystem


@dataclass
class Conversation:
    """会话（数据库，内存模拟）：消息表 + run 表。所有 server 实例共用。"""

    thread_id: str
    user_id: str
    messages: list[BaseMessage] = field(default_factory=list)
    runs: dict[str, str] = field(default_factory=dict)

    @property
    def prefix(self) -> str:
        return f"{self.user_id}/{self.thread_id}/workspace/"


class ServerInstance:
    def __init__(
        self,
        name: str,
        *,
        ensurer: Ensurer,
        storage: ObjectStorage,
        transport: httpx.AsyncBaseTransport,
    ) -> None:
        self.name, self._ensurer, self._storage, self._transport = name, ensurer, storage, transport
        self.memory = ProcessMemory()

    async def run_turn(self, db: Conversation, run_id: str) -> None:
        enter(run_id)
        sandbox = PodSandbox(
            ensurer=self._ensurer,
            workspace_thread_id=db.thread_id,  # 子智能体传父会话的工作区：共用一个沙箱
            user_id=db.user_id,
            run_id=run_id,
            memory=self.memory,
            transport=self._transport,
        )
        fs = WorkspaceFilesystem(
            ws=db.thread_id,
            oss=OssStore(self._storage, db.prefix),
            ensurer=self._ensurer,
            transport=self._transport,
        )
        agent = build_agent(sandbox, fs)
        db.runs[run_id] = f"running（实例 {self.name}）"
        latest = list(db.messages)
        async for state in agent.astream({"messages": latest}, stream_mode="values"):
            latest = state["messages"]
        db.messages = latest
        db.runs[run_id] = f"succeeded（实例 {self.name}）"
        say("执行器", f"{run_id} 结束（实例 {self.name}），消息落库")
