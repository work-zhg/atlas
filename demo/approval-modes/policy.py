"""判定规则：工具的副作用类别 × 当前模式 → 放行 / 请示 / 判断 / 拦下（设计文档第 5 节）。

★ 判定看的是工具的**属性**（这里的 TOOL_EFFECTS 表），不按名字前缀猜。
  真实系统里内置工具在注册表里声明，MCP 工具取官方注解 readOnlyHint；查不到的按 external 处理。
"""

from __future__ import annotations

from enum import Enum

from state import Mode


class Effect(Enum):
    READ = "只读"
    PLAN = "计划状态"
    WRITE = "工作区写入"
    EXECUTE = "命令执行"
    EXTERNAL = "外部副作用"
    DELEGATE = "委派"


class Decision(Enum):
    ALLOW = "放行"
    ASK = "请示"
    JUDGE = "判断"
    BLOCK = "拦下"


TOOL_EFFECTS: dict[str, Effect] = {
    "read_file": Effect.READ,
    "exit_plan_mode": Effect.PLAN,
    "write_file": Effect.WRITE,
    "execute": Effect.EXECUTE,
}

_ALLOW, _ASK, _JUDGE, _BLOCK = Decision.ALLOW, Decision.ASK, Decision.JUDGE, Decision.BLOCK

#: 判定矩阵（设计文档 5.2）。demo 只有前四类工具，后两类照抄设计，便于对照
MATRIX: dict[Mode, dict[Effect, Decision]] = {
    "plan": {
        Effect.READ: _ALLOW, Effect.PLAN: _ALLOW, Effect.WRITE: _BLOCK,
        Effect.EXECUTE: _BLOCK, Effect.EXTERNAL: _BLOCK, Effect.DELEGATE: _BLOCK,
    },
    "manual": {
        Effect.READ: _ALLOW, Effect.PLAN: _ALLOW, Effect.WRITE: _ASK,
        Effect.EXECUTE: _ASK, Effect.EXTERNAL: _ASK, Effect.DELEGATE: _ASK,
    },
    "auto": {
        Effect.READ: _ALLOW, Effect.PLAN: _ALLOW, Effect.WRITE: _ALLOW,
        Effect.EXECUTE: _JUDGE, Effect.EXTERNAL: _JUDGE, Effect.DELEGATE: _JUDGE,
    },
}  # fmt: skip


def effect_of(tool: str) -> Effect:
    """查不到属性的工具按最保守的「外部副作用」处理。"""
    return TOOL_EFFECTS.get(tool, Effect.EXTERNAL)


def decide(mode: Mode, tool: str) -> tuple[Effect, Decision]:
    effect = effect_of(tool)
    return effect, MATRIX[mode][effect]
