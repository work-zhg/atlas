"""浏览器的接口（c4-model/agent/browser/design-browser.html）：接管、我的登录、浏览器状态。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class TakeoverNote(BaseModel):
    """交还、不处理时人写的一句说明（交给模型）。"""

    note: str | None = Field(default=None, max_length=500)


class TakeoverLoginOut(BaseModel):
    #: 这次登录的站点存下来了没有（存失败也照样交还）
    saved: bool
    sites: list[str] = []


class TakeoverOut(BaseModel):
    approval_id: UUID
    run_id: UUID
    thread_id: UUID
    #: 打开：opened；交还：approved；不处理：rejected
    status: Literal["opened", "approved", "rejected"]
    login: TakeoverLoginOut | None = None


class BrowserLoginOut(BaseModel):
    """「我的登录」里的一行：没有内容，只有站点和时间。"""

    site: str
    captured_at: datetime
    #: 导出时所在的会话
    captured_from: UUID | None = None
    expires_at: datetime | None = None
    expired: bool


class BrowserLoginList(BaseModel):
    items: list[BrowserLoginOut]


class BrowserStatusOut(BaseModel):
    """这个会话的浏览器在不在跑：在跑才有画面（看画面不为此新建沙箱）。"""

    running: bool


class BrowserClipboard(BaseModel):
    """浏览器所在虚拟屏幕的剪贴板：人在画面里粘贴（写）、复制（读）。"""

    text: str = Field(max_length=100_000)
