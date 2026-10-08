"""会话与消息 API schema（文档 §11.2）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

#: 用户可以把会话设成的状态。
ThreadStatusIn = Literal["active", "archived"]

#: 会话实际可能处于的状态。
#:
#: ★ "ephemeral" 只属于**一次性子会话**，用户设不出来。它同时承担三件事：
#:   find_subagent 只认 active（于是一次性会话永不被「恢复」命中）、
#:   (parent, subagent_name) 的部分唯一索引只覆盖 active（于是同一个一次性
#:   子智能体可以真并行）、以及在界面上一眼区分「持续协作的助理」与
#:   「跑完就算」的一次性执行体。
ThreadStatus = Literal["active", "archived", "ephemeral"]
TitleSource = Literal["pending", "generated", "fallback", "manual"]


class ThreadCreate(BaseModel):
    agent_id: UUID
    title: str = Field(default="", max_length=256)


class ThreadUpdate(BaseModel):
    """重命名会把 title_source 置为 manual —— 之后永不被自动生成覆盖（决策 5）。"""

    title: str | None = Field(default=None, max_length=256)
    status: ThreadStatusIn | None = None


class ThreadRunState(BaseModel):
    """会话最近一个 run 的状态 —— 列表上的「运行中 / 待审批 / 等待子智能体 / 失败」。"""

    id: UUID
    #: queued | running | awaiting_approval | suspended |
    #: succeeded | failed | cancelled | interrupted
    status: str
    #: 挂起时在等什么：approval / delegation
    waiting: list[str] = []
    #: 需要用户处理：本 run 或它委派出去的子 run 在等审批
    needs_approval: bool = False


class ThreadOut(BaseModel):
    id: UUID
    agent_id: UUID
    agent_slug: str
    agent_name: str
    agent_avatar_key: str
    title: str
    title_source: TitleSource
    status: ThreadStatus
    #: 非空即「这是某个会话的子智能体会话」。子会话不出现在会话列表里，
    #: 只作为父会话详情页上的可展开分支（设计 §02）。
    parent_thread_id: UUID | None = None
    subagent_name: str | None = None
    #: 本会话下的子会话数。★ 删除确认要显示它：删父会话会**级联删掉**所有
    #: 子会话的 message / run / run_event，影响面比改造前大得多。
    #: 只在单条详情上计算，列表接口恒为 0（避免 N+1）。
    subagent_thread_count: int = 0
    #: 当前还在跑的 run。刷新页面后前端据此恢复事件流 —— 断线续传的
    #: 机制（Last-Event-ID + run_event 归档）本来就有，缺的只是入口。
    #: 与 subagent_thread_count 同款：只在单条详情上计算，列表恒为 None。
    active_run_id: UUID | None = None
    #: 最近一个 run 的状态（列表与详情都给；一页两次查询，不 N+1）
    last_run: ThreadRunState | None = None
    message_count: int
    compact_count: int
    latest_state: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class ThreadListOut(BaseModel):
    data: list[ThreadOut]
    next_cursor: str | None = None


class MessageOut(BaseModel):
    id: UUID
    thread_id: UUID
    run_id: UUID | None
    role: Literal["user", "assistant"]
    content: list[dict[str, Any]]
    created_at: datetime


class WorkspaceFile(BaseModel):
    #: 相对工作区根的路径，如 assets/app.js
    path: str
    size: int
    modified_at: str | None = None
    #: 对象存储的 ETag（去引号）。预览据此判断内容是否变了
    etag: str | None = None


class WorkspaceFilesOut(BaseModel):
    """会话工作区里的文件（右侧「文件」面板）。子会话看的是父会话的工作区（共享）。"""

    #: False = 没配对象存储：这个部署没有文件能力（不是「还没有文件」）
    configured: bool
    data: list[WorkspaceFile]
    #: 文件太多，只列了前一部分
    truncated: bool = False


class PreviewSessionOut(BaseModel):
    """文件预览站点的入口。base_url 以 / 结尾，文件地址 = base_url + 逐段编码的路径。"""

    base_url: str
    #: 空闲到这个时间点就失效；每次访问都会续期
    expires_at: datetime


class MessageListOut(BaseModel):
    """倒序分页：最新的在前，前端向上滚动加载更旧的。"""

    data: list[MessageOut]
    next_cursor: str | None = None
