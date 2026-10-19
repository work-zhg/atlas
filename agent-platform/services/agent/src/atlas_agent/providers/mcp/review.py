"""MCP 工具的复核状态与定义漂移（技能 / MCP 设计 §8.3、§9）。

两件事，两个不同的问题：

  复核状态  这一版定义**有没有被人看过**（全局，配置服务记录 approved / rejected）
  定义漂移  这一版定义与 agent **保存时**看到的是否一致（每个 agent 版本各自记录）

★ 只改描述不改名字是注入风险最高的变更（MCP 概设 §08）—— digest 覆盖了描述，
  所以两者都按 digest 判，而不是按工具名集合。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from .catalog import McpToolDef
from .config import McpServerConfig

__all__ = ["ReviewIndex", "ReviewStatus", "review_status"]

#: server → {(线上名, digest): "approved" | "rejected"}
ReviewIndex = Mapping[str, Mapping[tuple[str, str], str]]
ReviewStatus = Literal["ok", "invalid", "pending_review", "rejected"]


def review_status(
    config: McpServerConfig, tool: McpToolDef, reviews: ReviewIndex | None
) -> ReviewStatus:
    if not tool.usable:
        return "invalid"
    decision = (reviews or {}).get(config.name, {}).get((tool.name, tool.digest))
    if decision == "rejected":
        return "rejected"
    if decision == "approved" or not config.review_required:
        return "ok"
    return "pending_review"
