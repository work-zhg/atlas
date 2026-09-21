"""★ 上下文自动压缩的切分逻辑（文档 §7）。

本模块只做**一件危险的事**：决定从哪里切。切错了 API 直接 400。

── §7.3 坑 1：tool_use / tool_result 必须成对 ──────────────────────────────
Anthropic 严格校验：assistant 消息里的每个 tool_use block，必须在紧随的
user 消息里有对应 tool_id 的 tool_result。压缩边界若正好切在两者之间，
请求会被拒。

所以**切分点不能按 token 数硬切，必须落在"完整回合"边界上** ——
即所有已发出的 tool_use 都已被回应的位置。

这个坑在短对话里永远暴露不出来（没有工具调用就没有配对可切断），
只能靠"构造 30 轮带工具调用的历史强制触发压缩"的测试兜住。

设计取舍：把危险逻辑做成**纯函数**，只依赖 MessageFacts 这个中立表示，
不依赖 LangChain 的具体版本与类型。版本相关的提取器另放（见 facts_from）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class MessageFacts:
    """压缩决策需要知道的全部信息 —— 与消息的具体类型无关。

    starts_turn: 是否是**真实用户输入**（开启一个新回合）。
                 关键区分：Anthropic 里 tool_result 也走 user 角色，
                 但它是上一回合的延续，不是新回合。把两者混为一谈会
                 把一个逻辑回合数成好几个，导致该保留的历史被压掉。
    issues:      这条消息**发出**的 tool_use id
    answers:     这条消息**回应**的 tool_result id
    """

    index: int
    starts_turn: bool = False
    is_thinking_only: bool = False
    issues: frozenset[str] = field(default_factory=frozenset)
    answers: frozenset[str] = field(default_factory=frozenset)


def safe_boundaries(facts: Sequence[MessageFacts]) -> list[int]:
    """所有可安全切分的下标。

    下标 i 安全 ⟺ facts[0:i] 内发出的每个 tool_use 都在 facts[0:i] 内被回应。
    0 与 len(facts) 恒为安全边界。
    """
    boundaries: list[int] = [0]
    outstanding: set[str] = set()

    for i, fact in enumerate(facts):
        outstanding -= set(fact.answers)
        outstanding |= set(fact.issues)
        # i+1 是"这条消息之后"的切点
        if not outstanding:
            boundaries.append(i + 1)

    if boundaries[-1] != len(facts):
        # 末尾仍有未闭合的 tool_use（本轮还没跑完），不是安全切点
        pass
    return boundaries


def plan_split(facts: Sequence[MessageFacts], *, keep_recent_turns: int) -> int:
    """返回切分下标：[0, idx) 进压缩区，[idx, len) 原样保留。

    idx 一定是安全边界。keep_recent_turns 指保留多少个完整回合；
    若历史太短或找不到合适的安全点，返回 0（表示不压缩）。
    """
    if keep_recent_turns < 1:
        raise ValueError("keep_recent_turns 至少为 1")

    starts = [i for i, f in enumerate(facts) if f.starts_turn]
    if len(starts) <= keep_recent_turns:
        return 0  # 回合数还不够多，没什么可压的

    # 想切在"倒数第 K 个回合"的开头
    candidate = starts[len(starts) - keep_recent_turns]

    # 但该位置未必安全（可能正卡在未闭合的工具调用中间）——
    # 向前回退到最近的安全边界。宁可少压一点，也不能拆散配对。
    safe = set(safe_boundaries(facts))
    while candidate > 0 and candidate not in safe:
        candidate -= 1
    return candidate


def would_split_tool_pair(facts: Sequence[MessageFacts], index: int) -> bool:
    """诊断用：在 index 处切会不会拆散 tool_use/tool_result 对。

    测试用它断言"朴素的按条数切"确实是危险的，从而证明本模块存在的必要。
    """
    outstanding: set[str] = set()
    for fact in facts[:index]:
        outstanding -= set(fact.answers)
        outstanding |= set(fact.issues)
    return bool(outstanding)


def drop_thinking(facts: Sequence[MessageFacts], upto: int) -> list[int]:
    """§7.3 坑 3：压缩区内的 thinking block 直接丢弃，不送去摘要。

    早期轮次的 thinking 对后续推理价值极低（结论已在正文里），
    且经网关时其文本恒为空。返回应当剔除的下标。
    """
    return [f.index for f in facts[:upto] if f.is_thinking_only]
