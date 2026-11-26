"""知识库的库级权限 = 用户中心数据权限（数据编码 AtlasKnowledgeBase）。

级别：READ 看到、检索、召回测试 < WRITE 上传、编辑分段、改检索设置 < OWNER 删除、共享。
「全部知识库 · Owner」是用户中心的数据编码级授权（取代原 kb:manage_all 操作码）。

★ 公有的库不靠这里：可见性记在库上（kb_dataset.visibility），本服务自己判 —— 任何人至少可读。
★ 私有的库只按这里判：不按角色、部门、本人自己推断，也没有文档级权限（能读库就能读库里全部文档）。

两个实现：
  UserCenterGateway   用户中心开放接口（usercenter.py，调用户中心的唯一客户端）
  测试里的内存实现      tests/conftest.py
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

__all__ = ["DATA_CODE", "LEVEL_RANK", "DataPermissions", "Grant"]

DATA_CODE = "AtlasKnowledgeBase"
LEVEL_RANK = {"NONE": 0, "READ": 1, "WRITE": 2, "OWNER": 3}


@dataclass(frozen=True)
class Grant:
    level: str  # READ | WRITE | OWNER
    subject_type: str  # user | dept
    subject_id: str
    include_sub: bool = False


class DataPermissions(Protocol):
    async def level(self, user_id: str, dataset_id: str) -> str:
        """有效级别（含「全部知识库」授权）。"""
        ...

    async def accessible(self, user_id: str, min_level: str = "READ") -> tuple[dict[str, str], str]:
        """→ ({数据 id: 级别}, 全部知识库的级别)。"""
        ...

    async def create(self, dataset_id: str, name: str, grants: list[Grant]) -> None:
        """新库首次写入（须含一条 OWNER）。"""
        ...

    async def acl(self, dataset_id: str) -> list[dict[str, Any]]: ...

    async def grant(self, dataset_id: str, operator: str, grants: list[Grant]) -> None: ...

    async def update(
        self, acl_id: str, operator: str, *, level: str | None, include_sub: bool | None
    ) -> None: ...

    async def revoke(self, acl_id: str, operator: str) -> None: ...

    async def rename(self, dataset_id: str, name: str) -> None: ...

    async def cleanup(self, dataset_id: str) -> None: ...
