"""工作空间（一个人的全部文件，{user}/files/）的接口模型。路径一律相对 files/，根目录是空串。"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field


class WorkspaceOut(BaseModel):
    """概况：占用、配额、单个文件上传上限（运行期配置 workspace.*）。"""

    #: False = 没配对象存储：这个部署没有文件能力
    configured: bool
    used_bytes: int = 0
    files: int = 0
    #: 0 = 不限
    quota_bytes: int = 0
    upload_max_bytes: int = 0


class WorkspaceFolder(BaseModel):
    name: str
    path: str
    #: 里面的文件数（递归，不含隐藏的，除非要了隐藏文件）
    files: int
    size: int
    modified_at: str | None = None


class WorkspaceEntryFile(BaseModel):
    name: str
    path: str
    size: int
    modified_at: str | None = None
    etag: str | None = None


class WorkspaceDirOut(BaseModel):
    dir: str
    dirs: list[WorkspaceFolder]
    files: list[WorkspaceEntryFile]
    #: 对象太多，只统计了前一部分
    truncated: bool = False


class WorkspaceSearchFolder(BaseModel):
    name: str
    path: str


class WorkspaceSearchOut(BaseModel):
    dirs: list[WorkspaceSearchFolder]
    files: list[WorkspaceEntryFile]
    truncated: bool = False


class WorkspaceRefThread(BaseModel):
    id: UUID
    title: str
    workspace_dir: str
    #: 有没有没结束的 run（整棵会话树）
    running: bool


class WorkspaceRefsOut(BaseModel):
    """把这个路径（或它下面的目录）当工作目录的会话（根会话）。"""

    threads: list[WorkspaceRefThread]


class WorkspaceFolderIn(BaseModel):
    path: str = Field(min_length=1, max_length=1024)


class WorkspaceMoveIn(BaseModel):
    src: str = Field(min_length=1, max_length=1024)
    dst: str = Field(min_length=1, max_length=1024)


class WorkspacePathOut(BaseModel):
    path: str
    size: int | None = None
