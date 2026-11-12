"""一个人的文件在对象存储里的布局（c4-model/agent/code-workspace.html · WorkspaceLayout）。

    {user}/                         整个挂到这个人的沙箱的 /data（唯一的挂载）
      files/                        用户的文件（工作空间页面的根）：普通的目录树
        {工作目录}/                 一个会话的工作目录：建会话时定下、存在会话记录里
                                    （thread.workspace_dir），之后不变；子孙会话用根会话的。
                                    也是命令的 cwd
          .atlas/{会话}/            本会话的压缩归档、外置的大结果（隐藏目录，跟着工作目录走）
      skills/{slug}/{版本}/         技能：这个人所有会话共用，同一技能的不同版本并排放
      home/.claude/                 CLI 的会话记录和配置：一人一份，不属于任何会话

★ 工作目录：建会话时指定了就用指定的（工作空间里任意一个目录，多个会话可以共用）；没指定就是
  {日期}/{会话 id}。路径相对 files/。
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
from datetime import date
from enum import StrEnum
from uuid import UUID

__all__ = [
    "DATA_MOUNT",
    "FILES_DIR",
    "HOME_CLAUDE",
    "WORKSPACE_DIR_MAX",
    "Area",
    "InvalidWorkspaceDir",
    "WorkspaceLayout",
    "default_workspace_dir",
    "normalize_workspace_dir",
]

#: 整个 {user}/ 在沙箱里的挂载点；模型看到的绝对路径都在它下面
DATA_MOUNT = "/data"
#: 用户的文件（相对这个人的前缀）：工作目录都在它下面
FILES_DIR = "files"
#: CLI 的会话记录和配置（相对这个人的前缀）
HOME_CLAUDE = "home/.claude"
#: 工作目录路径的长度上限（thread.workspace_dir 的列宽）
WORKSPACE_DIR_MAX = 512
#: 本会话的隐藏目录（在所用工作目录里）
_ATLAS_DIR = ".atlas"
#: 这个人前缀下第一层只有这三个
_SKILLS_DIR = "skills"
_HOME_DIR = HOME_CLAUDE.split("/", 1)[0]
_TOP_LEVEL = frozenset({FILES_DIR, _SKILLS_DIR, _HOME_DIR})


class InvalidWorkspaceDir(ValueError):
    """用户给的工作目录不合法：空（= 整个 files/）、越界、点开头（隐藏目录）、太长。"""


def default_workspace_dir(thread_id: UUID | str, day: date) -> str:
    """没指定工作目录的会话用的目录：{日期}/{会话 id}（相对 files/）。会话 id 唯一，不会撞名。"""
    return f"{day.isoformat()}/{thread_id}"


def normalize_workspace_dir(raw: str) -> str:
    """用户给的工作目录（相对 files/）→ 规范形式：去掉多余的 / 和 . 段。

    ★ 不能是 files/ 本身：那等于把这个人的全部文件交给智能体。
    ★ 不能有点开头的段：那是隐藏目录（.atlas 是会话的内部记录）。
    """
    if "\x00" in raw or "\\" in raw:
        raise InvalidWorkspaceDir(f"工作目录含非法字符：{raw!r}")
    parts = [p for p in raw.strip().split("/") if p.strip() not in ("", ".")]
    if not parts:
        raise InvalidWorkspaceDir("工作目录不能是工作空间的根目录")
    for part in parts:
        if part == "..":
            raise InvalidWorkspaceDir(f"工作目录不能含 ..：{raw!r}")
        if part.startswith("."):
            raise InvalidWorkspaceDir(f"工作目录不能是隐藏目录：{raw!r}")
    out = "/".join(parts)
    if len(out) > WORKSPACE_DIR_MAX:
        raise InvalidWorkspaceDir(f"工作目录太长（超过 {WORKSPACE_DIR_MAX} 个字符）")
    return out


class Area(StrEnum):
    """文件工具能用的根：所用工作区、本智能体绑定的技能版本。"""

    WORKSPACE = "workspace"
    SKILLS = "skills"


@dataclass(frozen=True)
class WorkspaceLayout:
    """一个会话看到的布局。

    workspace_dir：所用工作目录，相对 files/（子、孙会话传根会话的）。空串 = files/ 本身 ——
        只给工作空间页面用（列这个人的全部文件），会话一定有自己的工作目录。
    """

    user_id: str
    thread_id: str
    workspace_dir: str

    @classmethod
    def of(cls, user_id: UUID | str, thread_id: UUID | str, workspace_dir: str) -> WorkspaceLayout:
        return cls(str(user_id), str(thread_id), workspace_dir.strip("/"))

    # ------------------------------------------------------------------ 相对这个人前缀的路径

    @property
    def workspace(self) -> str:
        """所用工作目录。"""
        return f"{FILES_DIR}/{self.workspace_dir}" if self.workspace_dir else FILES_DIR

    @property
    def atlas_dir(self) -> str:
        """本会话的隐藏目录：压缩归档、外置的大结果。"""
        return f"{self.workspace}/{_ATLAS_DIR}/{self.thread_id}"

    @staticmethod
    def skill(slug: str, version: int | str) -> str:
        """一个技能版本的目录。"""
        return f"skills/{slug}/{version}"

    @staticmethod
    def is_top_level(name: str) -> bool:
        """name 是不是这个人前缀下第一层的东西：用户文件、技能目录、home。"""
        return name in _TOP_LEVEL

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
