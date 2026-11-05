"""路径关卡（c4-model/agent/code-workspace.html · PathGate）。

把模型（或界面）给的路径换成「哪个区 + 区内相对路径」：

    /skills/...           技能区（相对的 skills/... 历史上也算，保持不变）
    /workspace/...        工作区区，剥掉挂载点（只剥绝对路径：相对的 workspace/a.md 是工作区里一个
                          叫 workspace 的子目录）
    /large_tool_results/... 和 /conversation_history/...
                          系统区，保留目录名（引擎外置大结果 / 被压缩的历史用这两个保留目录）
    其余                  工作区区

★ 外置的东西放系统区：模型看到的路径不变、照样能读，但不进工作区 —— 不出现在界面的文件面板里，
  也不挂进沙箱（沙箱里的进程碰不到）。原先它们落在工作区，和用户的文件混在一起。

★ 会话隔离全靠这一道关卡。它是本组件唯一的安全关键点 —— 写错了等于会话之间可以互相读写。
★ 先规范化、再拼前缀：先拼后归一的话，../../other-thread/x 会被归一成另一个会话的真实 key，
  从调用方看毫无异常。越出区根的路径直接拒绝（原先是悄悄归进工作区：模型以为读写的是
  ../x，实际落在别处，它收不到任何提示）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ...domain.workspace_layout import SKILLS_MOUNT, WORKSPACE_MOUNT, Area

__all__ = ["SYSTEM_DIRS", "FilePath", "PathEscape", "PathGate"]


class PathEscape(ValueError):
    """路径不合法：空字节、越出区根、指向区根本身。

    不是「文件不存在」那类可恢复错误 —— 它要么是模型在乱试，要么是调用方拼错了路径。两种都该
    显式失败，而不是悄悄落到别的地方。
    """


@dataclass(frozen=True)
class FilePath:
    """关卡放行后的路径：哪个区、区内相对路径（不带开头的 /；前缀可以以 / 结尾，区根是空串）。"""

    area: Area
    rel: str

    def in_sandbox(self) -> bool:
        """只有工作区区会走沙箱：技能区在沙箱里只读，系统区根本不挂进沙箱。"""
        return self.area is Area.WORKSPACE


def _components(path: str, original: str) -> list[str]:
    """按 / 切开并规范化 . 和 ..；.. 越过根就是越界。"""
    out: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not out:
                raise PathEscape(f"路径越出了工作区：{original!r}")
            out.pop()
            continue
        out.append(part)
    return out


_SKILLS = SKILLS_MOUNT.strip("/")
_WORKSPACE = WORKSPACE_MOUNT.strip("/")
#: 系统区里模型可读写的保留目录（与 atlas_engine 的 OFFLOAD_DIR / INPUT_DIR 对应）
SYSTEM_DIRS = frozenset({"large_tool_results", "conversation_history"})


class PathGate:
    def resolve(self, path: str) -> FilePath:
        """一个文件的路径。指向区根本身（工作区、技能区）也拒绝：读写它没有意义。"""
        fp = self._resolve(path)
        if not fp.rel:
            raise PathEscape(f"路径必须指向一个文件，而不是工作区本身：{path!r}")
        return fp

    def resolve_prefix(self, prefix: str) -> FilePath:
        """列举用的前缀。空串和 / 是工作区区的根；以 / 结尾的保留结尾的 /。"""
        fp = self._resolve(prefix)
        trailing = prefix.endswith("/") and fp.rel != ""
        return FilePath(fp.area, fp.rel + "/" if trailing else fp.rel)

    @staticmethod
    def shown(fp: FilePath) -> str:
        """区内路径 → 模型看到的路径（search 的结果用）。技能区带上 skills/ 根；系统区的 rel
        本来就以保留目录开头。"""
        return f"{_SKILLS}/{fp.rel}" if fp.area is Area.SKILLS else fp.rel

    def _resolve(self, path: str) -> FilePath:
        if "\x00" in path:
            raise PathEscape(f"路径含空字节：{path!r}")
        absolute = path.startswith("/")
        parts = _components(path, path)
        if parts and parts[0] == _SKILLS:
            return FilePath(Area.SKILLS, "/".join(parts[1:]))
        if absolute and parts and parts[0] == _WORKSPACE:
            return FilePath(Area.WORKSPACE, "/".join(parts[1:]))
        if absolute and parts and parts[0] in SYSTEM_DIRS:
            return FilePath(Area.SYSTEM, "/".join(parts))
        return FilePath(Area.WORKSPACE, "/".join(parts))
