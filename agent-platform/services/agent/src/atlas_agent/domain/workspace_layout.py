"""一个人的文件在对象存储里的布局（c4-model/agent/code-workspace.html · WorkspaceLayout）。

    {user}/                         整个挂到这个人的沙箱的 /data（唯一的挂载）
      {根会话}/workspace/           工作区：根会话和它的子孙会话共用，也是命令的 cwd
        .atlas/{会话}/              本会话的压缩归档、外置的大结果（隐藏目录，跟着工作区走）
      skills/{slug}/{版本}/         技能：这个人所有会话共用，同一技能的不同版本并排放
      home/.claude/                 CLI 的会话记录和配置：一人一份，不属于任何会话

★ 路径就是沙箱里的真实路径：相对这个人前缀的路径 rel，在沙箱里是 /data/{rel}，在对象存储里是
  {user}/{rel}，一一对应，不用查表。文件工具和 Shell 看到的是同一套。
★ 挂整个 {user}/：Pod 的卷在创建时就定死了，按会话挂 subPath 的话，后加入的会话（子会话、这个人
  新开的会话）挂不上自己的目录；挂父前缀，新目录一建好 Pod 里就看得见。
★ 共享内核：直连对象存储的 key、原生沙箱和会话 Pod 的挂载都从这里取，只算一次。放进工作区组件又会
  和沙箱互相依赖（工作区要问沙箱选路，沙箱要按布局挂载），所以单独放在两者之下 —— 纯计算，没有依赖。
★ 前缀里有 user 段：路径越界因此也挡住了跨用户。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

__all__ = ["DATA_MOUNT", "HOME_CLAUDE", "Area", "WorkspaceLayout"]

#: 整个 {user}/ 在沙箱里的挂载点；模型看到的绝对路径都在它下面
DATA_MOUNT = "/data"
#: CLI 的会话记录和配置（相对这个人的前缀）
HOME_CLAUDE = "home/.claude"
#: 本会话的隐藏目录（在所用工作区里）
_ATLAS_DIR = ".atlas"
#: 这个人前缀下第一层的两个固定目录（其余是根会话的 id）
_SKILLS_DIR = "skills"
_HOME_DIR = HOME_CLAUDE.split("/", 1)[0]


class Area(StrEnum):
    """文件工具能用的根：所用工作区、本智能体绑定的技能版本。"""

    WORKSPACE = "workspace"
    SKILLS = "skills"


@dataclass(frozen=True)
class WorkspaceLayout:
    """一个会话看到的布局。

    workspace_thread_id：所用工作区的根会话（子、孙会话传根会话的，没有自己的工作区）。
    """

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

    # ------------------------------------------------------------------ 相对这个人前缀的路径

    @property
    def workspace(self) -> str:
        """所用工作区。"""
        return f"{self.workspace_thread_id}/workspace"

    @property
    def atlas_dir(self) -> str:
        """本会话的隐藏目录：压缩归档、外置的大结果。"""
        return f"{self.workspace}/{_ATLAS_DIR}/{self.thread_id}"

    @staticmethod
    def skill(slug: str, version: int | str) -> str:
        """一个技能版本的目录。"""
        return f"skills/{slug}/{version}"

    def is_top_level(self, name: str) -> bool:
        """name 是不是这个人前缀下第一层的东西：技能目录、home、会话（根会话的 id）。"""
        if name in (_SKILLS_DIR, _HOME_DIR, self.workspace_thread_id):
            return True
        try:
            UUID(name)
        except ValueError:
            return False
        return True

    # ------------------------------------------------------------------ 换算

    @property
    def user_prefix(self) -> str:
        """这个人在对象存储里的前缀（不带结尾的 /）：整个挂进沙箱的 subPath。"""
        return self.user_id

    def key(self, rel: str) -> str:
        """相对路径 → object key。rel 为空时是这个人的前缀加 /（列举用）。"""
        return f"{self.user_id}/{rel}"

    @staticmethod
    def real(rel: str) -> str:
        """相对路径 → 沙箱里的真实路径。"""
        return f"{DATA_MOUNT}/{rel}" if rel else DATA_MOUNT

    @property
    def workdir(self) -> str:
        """所用工作区在沙箱里的路径：命令的 cwd，也是 CLI 的 cwd。"""
        return self.real(self.workspace)
