"""会话的文件在对象存储里的布局（c4-model/agent/code-workspace.html · WorkspaceLayout）。

    {主体}/                         文件主体的前缀，整个挂到沙箱的 /data。主体：以人为权限是会话的
                                    创建人，以数字员工为权限是数字员工（建会话时定，thread.owner_uuid）
      files/                        用户的文件（工作空间页面的根）：普通的目录树
        {工作目录}/                 一个会话的工作目录：建会话时定下、存在会话记录里
                                    （thread.workspace_dir），之后不变；子孙会话用根会话的。
                                    也是命令的 cwd
          .atlas/{会话}/            本会话的压缩归档、外置的大结果（隐藏目录，跟着工作目录走）
      skills/{slug}/{版本}/         技能：这个人所有会话共用，同一技能的不同版本并排放
      home/.claude/                 CLI 的会话记录和配置：一人一份，不属于任何会话
    {应用}/{编号}/                  应用工作空间（如 TeamFlow 的流程空间）：不属于任何主体，只挂给以
                                    数字员工为权限的会话，挂到 /space；用它的会话把它当工作目录

★ 工作目录：建会话时指定了就用指定的（工作空间里任意一个目录，多个会话可以共用）；没指定就是
  workspace（原生、会话 Pod 里的 CLI 都一样，没指定目录的会话共用它）。路径相对 files/。
★ 路径就是沙箱里的真实路径：相对这个人前缀的路径 rel，在沙箱里是 /data/{rel}，在对象存储里是
  {user}/{rel}，一一对应，不用查表。文件工具和 Shell 看到的是同一套。
★ 挂整个 {user}/：Pod 的卷在创建时就定死了，按会话挂 subPath 的话，后加入的会话（子会话、这个人
  新开的会话）挂不上自己的目录；挂父前缀，新目录一建好 Pod 里就看得见。
★ 共享内核：直连对象存储的 key、原生沙箱和会话 Pod 的挂载都从这里取，只算一次。放进工作区组件又会
  和沙箱互相依赖（工作区要问沙箱选路，沙箱要按布局挂载），所以单独放在两者之下 —— 纯计算，没有依赖。
★ 前缀里有 user 段：路径越界因此也挡住了跨用户。
★ 应用工作空间在相对路径里是保留的顶层段 space/：space/{rel} 在沙箱里是 /space/{rel}，在对象存储里是
  {应用}/{编号}/{rel}。主体前缀下第一层只有 files、skills、home，不会和它重名。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

__all__ = [
    "DATA_MOUNT",
    "DEFAULT_WORKSPACE_DIR",
    "FILES_DIR",
    "HOME_CLAUDE",
    "SPACE_DIR",
    "SPACE_MOUNT",
    "WORKSPACE_DIR_MAX",
    "Area",
    "InvalidWorkspaceDir",
    "WorkspaceLayout",
    "app_space_prefix",
    "normalize_workspace_dir",
]

#: 整个 {主体}/ 在沙箱里的挂载点
DATA_MOUNT = "/data"
#: 应用工作空间在沙箱里的挂载点
SPACE_MOUNT = "/space"
#: 应用工作空间在相对路径里的顶层段（保留名）
SPACE_DIR = "space"
#: 用户的文件（相对这个人的前缀）：工作目录都在它下面
FILES_DIR = "files"
#: 没指定工作目录的会话用的目录（相对 files/）：这些会话共用它
DEFAULT_WORKSPACE_DIR = "workspace"
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


def app_space_prefix(app: str, space_uuid: UUID | str) -> str:
    """应用工作空间在对象存储里的前缀（不带结尾的 /）：{应用}/{编号}，登记时定、不变。"""
    return f"{app}/{space_uuid}"


class InvalidWorkspaceDir(ValueError):
    """用户给的工作目录不合法：空（= 整个 files/）、越界、点开头（隐藏目录）、太长。"""


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

    user_id：文件主体（以数字员工为权限的会话是数字员工）。
    workspace_dir：所用工作目录，相对 files/（子、孙会话传根会话的）。空串 = files/ 本身 ——
        只给工作空间页面用（列这个人的全部文件），会话一定有自己的工作目录。
    space：用的应用工作空间的前缀（app_space_prefix）。给了它，工作目录就是它（workspace_dir
        不用），挂在 /space。
    """

    user_id: str
    thread_id: str
    workspace_dir: str
    space: str | None = None

    @classmethod
    def of(
        cls,
        user_id: UUID | str,
        thread_id: UUID | str,
        workspace_dir: str,
        *,
        space: str | None = None,
    ) -> WorkspaceLayout:
        return cls(str(user_id), str(thread_id), workspace_dir.strip("/"), space)

    # ------------------------------------------------------------------ 相对这个人前缀的路径

    @property
    def workspace(self) -> str:
        """所用工作目录。"""
        if self.space is not None:
            return SPACE_DIR
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

    def _in_space(self, rel: str) -> str | None:
        """rel 在应用工作空间里时，返回相对空间的部分（空间本身是空串）；不在返回 None。"""
        if self.space is None:
            return None
        if rel == SPACE_DIR:
            return ""
        return rel[len(SPACE_DIR) + 1 :] if rel.startswith(f"{SPACE_DIR}/") else None

    def key(self, rel: str) -> str:
        """相对路径 → object key。rel 为空时是这个人的前缀加 /（列举用）。"""
        inner = self._in_space(rel)
        if inner is not None:
            return f"{self.space}/{inner}"
        return f"{self.user_id}/{rel}"

    def rel_of_key(self, key: str) -> str:
        """object key → 相对路径（key 的反算，列举用）。"""
        if self.space is not None and key.startswith(f"{self.space}/"):
            return f"{SPACE_DIR}/{key[len(self.space) + 1 :]}"
        return key.removeprefix(f"{self.user_id}/")

    def real(self, rel: str) -> str:
        """相对路径 → 沙箱里的真实路径。"""
        inner = self._in_space(rel)
        if inner is not None:
            return f"{SPACE_MOUNT}/{inner}" if inner else SPACE_MOUNT
        return self.data_path(rel)

    def rel_of_real(self, path: str) -> str:
        """沙箱里的真实路径 → 相对路径（real 的反算）。"""
        if self.space is not None and (path == SPACE_MOUNT or path.startswith(f"{SPACE_MOUNT}/")):
            return f"{SPACE_DIR}/{path[len(SPACE_MOUNT) + 1 :]}".rstrip("/")
        return path.removeprefix(f"{DATA_MOUNT}/")

    @staticmethod
    def data_path(rel: str) -> str:
        """主体前缀下的相对路径 → 沙箱里 /data 下的真实路径（技能、home 都在这里）。"""
        return f"{DATA_MOUNT}/{rel}" if rel else DATA_MOUNT

    @property
    def workdir(self) -> str:
        """所用工作区在沙箱里的路径：命令的 cwd，也是 CLI 的 cwd。"""
        return self.real(self.workspace)
