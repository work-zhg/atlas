"""「我的设备」与新建本机 CLI 会话时选设备、选项目的接口形状（本机 CLI 方案 §2.2 · §2.3）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

__all__ = [
    "DeviceChoiceOut",
    "DeviceListOut",
    "DeviceOut",
    "DeviceRenameIn",
    "PairingCodeOut",
    "ThreadDeviceOut",
]


class PairingCodeOut(BaseModel):
    #: 8 位（中间一个短横线），10 分钟内有效、只能用一次
    code: str
    expires_at: datetime


class DeviceOut(BaseModel):
    id: UUID
    name: str
    is_default: bool
    #: online | offline（已吊销的不列）
    status: str
    os: str | None = None
    connector_version: str | None = None
    paired_at: datetime
    last_seen_at: datetime | None = None
    #: 在线时连接器报告的：装了哪些 CLI（名 → 版本）、本机策略摘要（允许的目录、权限模式上限……）
    clis: dict[str, str] = Field(default_factory=dict)
    policy: dict[str, Any] = Field(default_factory=dict)


class DeviceListOut(BaseModel):
    data: list[DeviceOut]


class DeviceRenameIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class DeviceChoiceOut(BaseModel):
    """新建本机 CLI 会话时的一台电脑：只有在线、装了这种 CLI 的能选（selectable）。"""

    id: UUID
    name: str
    is_default: bool
    online: bool
    has_cli: bool
    selectable: bool
    #: 本机允许平台使用的目录（项目目录必须在里面）
    allowed_roots: list[str] = Field(default_factory=list)


class ThreadDeviceOut(BaseModel):
    """本机 CLI 会话跑在哪：对话头部显示「设备名 · 本机路径」和在线状态。"""

    id: UUID
    name: str
    project_dir: str
    #: online | offline | revoked；列表接口不查（unknown）
    status: str = "unknown"
