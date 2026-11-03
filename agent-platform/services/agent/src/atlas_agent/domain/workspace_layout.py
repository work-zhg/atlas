"""会话文件的三个区在对象存储里的前缀（c4-model/agent/code-workspace.html · WorkspaceLayout）。

    工作区区   {user}/{根会话}/workspace   由根会话共享（子智能体和父会话共用），整个挂进
                                           沙箱 / 会话 Pod
    技能区     {user}/{本会话}/skills      按本会话，各有一套；沙箱里只读挂载
    系统区     {user}/{本会话}/system      按本会话：外置的大结果、CLI 的 HOME 等；不挂进原生沙箱

★ 共享内核：直连对象存储的 key、原生沙箱和会话 Pod 的挂载前缀都从这里取，只算一次。原先三处各拼
  一遍，靠注释「与 OssFilesystem 一致」维持。放进工作区组件又会和沙箱互相依赖（工作区要问沙箱
  选路，沙箱要按布局挂载），所以单独放在两者之下 —— 纯计算，没有任何依赖。
★ 三个区平级：工作区整个挂进沙箱时，技能和系统区不会被一起带进去。前缀里有 user 段，路径越界
  因此也挡住了跨用户。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

__all__ = ["SKILLS_MOUNT", "WORKSPACE_MOUNT", "Area", "WorkspaceLayout"]

#: 工作区在容器里的挂载点（原生沙箱、会话 Pod 都是它；模型看到的绝对路径也是它）
WORKSPACE_MOUNT = "/workspace"
#: 技能区在容器里的挂载点（只读）；模型用 /skills/... 读技能
SKILLS_MOUNT = "/skills"


class Area(StrEnum):
    WORKSPACE = "workspace"
    SKILLS = "skills"
    SYSTEM = "system"


@dataclass(frozen=True)
class WorkspaceLayout:
    """一个会话的三个区。workspace_thread_id：工作区归属的根会话（子智能体传父会话的）。"""

    user_id: str
    thread_id: str
    workspace_thread_id: str

    @classmethod
    def of(
        cls,
        user_id: UUID | str,
        thread_id: UUID | str,
        workspace_thread_id: UUID | str | None = None,
    ) -> WorkspaceLayout:
        return cls(str(user_id), str(thread_id), str(workspace_thread_id or thread_id))

    def prefix(self, area: Area) -> str:
        """区在对象存储里的前缀（不带结尾的 /）。也是挂载的 subPath。"""
        owner = self.workspace_thread_id if area is Area.WORKSPACE else self.thread_id
        return f"{self.user_id}/{owner}/{area.value}"

    def key(self, area: Area, rel: str) -> str:
        """区内相对路径 → object key。rel 为空时是区的前缀加 /（列举用）。"""
        return f"{self.prefix(area)}/{rel}"
