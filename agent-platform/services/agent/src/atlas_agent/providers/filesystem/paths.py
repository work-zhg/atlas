"""路径关卡（c4-model/agent/code-workspace.html · PathGate）。

把模型（或界面）给的路径换成「相对这个人前缀的路径 + 落在哪个根」：

    /data/...      沙箱里的真实路径，剥掉挂载点 —— 只在下一段是布局里的东西（skills、home、会话 id）
                   时；否则是工作区里一个叫 data 的子目录（见下）
    其余           相对所用工作区（引擎会把相对路径补成 / 开头，所以 /report.md 也按工作区算）

然后看落没落在本会话能用的根里：所用工作区（压缩归档、外置结果在其中的 .atlas/{会话}/），
以及本智能体绑定的技能版本 skills/{slug}/{版本}。别的会话的工作区、别的技能、home/ 都不放行 ——
文件工具只碰本会话该碰的东西（Shell 在同一个沙箱里看得到更多，那是会话之间不隔离的前提，
不由这里管）。

★ 相对路径 data/raw.csv 到这里已经是 /data/raw.csv（引擎补了 /）：raw.csv 不是布局里的东西，所以
  按工作区里的 data 子目录算。按挂载点算的话，它会被拒绝，而 data 是很常见的目录名。
★ 旧路径 /workspace/…、/skills/… 直接拒绝，报错里给出新路径：按「其余相对工作区」的规则，它们会悄悄
  落进工作区里一个叫 workspace 的子目录，模型以为写到了工作区根，Shell 里却找不到。相对的
  workspace/x 仍是工作区里的子目录（引擎给的路径都以 / 开头，旧路径一定是绝对的）。
★ 先规范化、再拼前缀：先拼后归一的话，../../other-thread/x 会被归一成另一个会话的真实 key，
  从调用方看毫无异常。越出根的路径直接拒绝（模型会收到提示，而不是落在别处）。
★ 会话之间的文件边界全靠这一道关卡，它是本组件唯一的安全关键点。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ...domain.workspace_layout import DATA_MOUNT, Area, WorkspaceLayout

__all__ = ["FilePath", "PathEscape", "PathGate"]

_DATA = DATA_MOUNT.strip("/")
#: 旧布局的挂载点：现在都在 /data 下面
_LEGACY_ROOTS = frozenset({"workspace", "skills"})


class PathEscape(ValueError):
    """路径不合法：空字节、越出根、不在本会话能用的根里、指向根本身。

    不是「文件不存在」那类可恢复错误 —— 它要么是模型在乱试，要么是调用方拼错了路径。两种都该
    显式失败，而不是悄悄落到别的地方。
    """


@dataclass(frozen=True)
class FilePath:
    """关卡放行后的路径：相对这个人前缀的路径（不带开头的 /；前缀可以以 / 结尾）和它落在的根。

    同一个路径在沙箱里是 /data/{rel}，在对象存储里是 {user}/{rel}：两种底层用的是同一个 rel。
    """

    area: Area
    rel: str

    @property
    def real(self) -> str:
        return WorkspaceLayout.real(self.rel)


def _normalize(parts: Iterable[str], original: str) -> list[str]:
    """规范化 . 和 ..；.. 越过起点就是越界。"""
    out: list[str] = []
    for part in parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not out:
                raise PathEscape(f"路径越出了可用的目录：{original!r}")
            out.pop()
            continue
        out.append(part)
    return out


class PathGate:
    def __init__(
        self, layout: WorkspaceLayout, skills: Iterable[tuple[str, int | str]] = ()
    ) -> None:
        """skills：本智能体绑定的技能 (slug, 版本)。"""
        self._layout = layout
        self._workspace = layout.workspace.split("/")
        self._roots: list[tuple[list[str], Area]] = [(self._workspace, Area.WORKSPACE)]
        self._roots += [(layout.skill(s, v).split("/"), Area.SKILLS) for s, v in skills]

    def resolve(self, path: str) -> FilePath:
        """一个文件的路径。指向根本身（工作区、技能目录）也拒绝：读写它没有意义。"""
        parts, area = self._resolve(path)
        if any(parts == root for root, _ in self._roots):
            raise PathEscape(f"路径必须指向一个文件，而不是目录本身：{path!r}")
        return FilePath(area, "/".join(parts))

    def resolve_prefix(self, prefix: str) -> FilePath:
        """列举用的前缀。空串和 / 是所用工作区的根；以 / 结尾的保留结尾的 /。"""
        parts, area = self._resolve(prefix)
        trailing = (
            prefix.endswith("/")
            or not prefix.strip("/")
            or any(parts == root for root, _ in self._roots)
        )
        return FilePath(area, "/".join(parts) + ("/" if trailing else ""))

    def shown(self, rel: str) -> str:
        """相对路径 → 模型看到的路径。

        工作区里的给相对工作区的路径（短；文件工具和 Shell 的 cwd 都认），工作区外的（技能）给沙箱里
        的真实路径。
        """
        parts = rel.split("/")
        if parts[: len(self._workspace)] == self._workspace:
            return "/".join(parts[len(self._workspace) :])
        return WorkspaceLayout.real(rel)

    def local(self, rel: str) -> str:
        """相对路径 → 相对它所在的根（工作区、技能目录）的路径。search 的 glob 也按它匹配。"""
        parts = rel.split("/")
        for root, _ in self._roots:
            if parts[: len(root)] == root:
                return "/".join(parts[len(root) :])
        return rel

    def _mounted(self, rest: list[str]) -> bool:
        """/data 后面的部分是不是指挂载点下的东西（第一段是布局里的：技能、home、会话 id）。"""
        first = next((p for p in rest if p != ".."), None)
        return first is None or ".." in rest[:1] or self._layout.is_top_level(first)

    def _resolve(self, path: str) -> tuple[list[str], Area]:
        if "\x00" in path:
            raise PathEscape(f"路径含空字节：{path!r}")
        raw = [p for p in path.split("/") if p not in ("", ".")]
        if path.startswith("/") and raw[:1] == [_DATA] and self._mounted(raw[1:]):
            parts = _normalize(raw[1:], path)
        elif path.startswith("/") and raw[:1] and raw[0] in _LEGACY_ROOTS:
            raise PathEscape(
                f"{path!r} 是旧的路径。工作区在 {self._layout.workdir}，"
                f"技能在 {DATA_MOUNT}/skills/<技能>/<版本>/（见技能清单里给的路径）"
            )
        else:
            parts = self._workspace + _normalize(raw, path)
        for root, area in self._roots:
            if parts[: len(root)] == root:
                return parts, area
        raise PathEscape(
            f"路径不在本会话能用的目录里：{path!r}。工作区是 {self._layout.workdir}，"
            f"技能只能用本智能体绑定的版本"
        )
