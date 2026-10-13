"""从工具事件里认出「技能被加载」（设计 §7.4）。

渐进式披露下，模型选中一个技能的唯一可观测信号是：它读了 `/skills/<slug>/SKILL.md`。
native 与 acp 的读文件工具都用 `file_path` 参数，挂载点也都是 /skills（native 由
OssFilesystem 路由、acp 由 Pod 模板挂载），所以一份规则两边共用。

★ 在 tool.completed 时才算：读失败（文件不存在、被拦）不算选中。
★ 纯函数 + 小状态机，不碰 IO —— 它在 domain 层（import-linter 守着）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from .events import EventType

__all__ = ["SkillLoadTracker", "skill_slug_of"]

_SKILL_MD = re.compile(r"^/?skills/([a-z][a-z0-9-]{0,62})/SKILL\.md$")
#: 读文件工具里可能装着路径的参数名（native: read_file.file_path；Claude Code: Read.file_path）
_PATH_KEYS = ("file_path", "path", "filePath")


def skill_slug_of(args: Any) -> str | None:
    if not isinstance(args, Mapping):
        return None
    for key in _PATH_KEYS:
        value = args.get(key)
        if isinstance(value, str) and (match := _SKILL_MD.match(value.strip())):
            return match.group(1)
    return None


class SkillLoadTracker:
    """看一条条事件，在合适的时候补出 skill.loaded。

    用法：每产出一个事件就 `observe(type, data)`，把返回的意图接着产出。
    同一个 run 里同一个技能只报一次（模型常会分几段读同一个 SKILL.md）。
    """

    def __init__(self, skills: Iterable[Any]) -> None:
        #: spec 里钉死的版本。不在 spec 里的技能（比如模型去读了别的路径）不报
        self._versions = {s.slug: s.version for s in skills}
        self._pending: dict[str, str] = {}
        self._reported: set[str] = set()

    def observe(
        self, kind: EventType, data: Mapping[str, Any]
    ) -> list[tuple[EventType, dict[str, Any]]]:
        if not self._versions:
            return []
        call_id = str(data.get("call_id") or "")
        if kind is EventType.TOOL_STARTED:
            slug = skill_slug_of(data.get("args"))
            if slug and slug in self._versions and call_id:
                self._pending[call_id] = slug
            return []
        if kind is EventType.TOOL_FAILED:
            self._pending.pop(call_id, None)
            return []
        if kind is EventType.TOOL_COMPLETED:
            slug = self._pending.pop(call_id, None)
            if slug is None or slug in self._reported:
                return []
            self._reported.add(slug)
            return [
                (
                    EventType.SKILL_LOADED,
                    {"slug": slug, "version": self._versions[slug], "call_id": call_id},
                )
            ]
        return []
