"""ACP v1 的内容 → 平台事件意图（Bridge 设计 §3 D2；代码设计 §11）。

server 里唯一理解 ACP 内容的地方。bridge 把 agent 的 session/update 一字不改地转交过来（B2），
这里直接读原始 JSON，不经模型往返 —— 与 bridge 读控制信号的方式一致（代码设计 §3）。

验收标准是 **web 一行不改**：前端的 reducer 按字段名取值，所以每种输入产出的 ``data``
形状与 native 路径逐字段一致；少一个字段就是「前端要加分支」。

返回 ``(EventType, data)`` 意图，而不是事件：seq 的单调无空洞由 EventFactory 独占，
翻译层拿不到工厂，就不可能破坏它。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from ...domain.events import Answer, EventType
from ...domain.translator import preview_args

__all__ = [
    "STOP_REASONS",
    "AcpV1Translator",
    "EventIntent",
    "model_of",
    "translate_crash",
    "translate_stop",
    "translate_update",
    "usage_of",
]

logger = logging.getLogger(__name__)

EventIntent = tuple[EventType, dict[str, Any]]

#: ACP v1 的全部结束原因
STOP_REASONS: frozenset[str] = frozenset(
    {"end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled"}
)

_RESULT_PREVIEW = 200

#: 只有终态产事件：前端的工具行已由 tool_call 建好，中间态只会让它闪烁
_TOOL_TERMINAL = {
    "completed": EventType.TOOL_COMPLETED,
    "failed": EventType.TOOL_FAILED,
}

#: usage.updated 的键名与 native 的 normalize_usage 一致（前端不需要分支）。
#: 值是 adapter 可能用的字段名，按顺序取第一个出现的。
#:
#: ★ 缓存字段有两套名字：旧 adapter（claude-code-acp）叫 cacheRead / cacheCreation，
#:   新 adapter（claude-agent-acp 0.85）叫 cachedReadTokens / cachedWriteTokens。
#:   只认旧名的话，新 adapter 的缓存 token 全记成 0 —— 费用系统性偏低，且不报错。
#: ★ 口径换算：adapter 的 inputTokens **不含**缓存部分（Anthropic API 的口径；实测
#:   input + output + cachedRead == total），而 native 的 normalize_usage（LangChain）与
#:   OTel GenAI 语义约定里 input_tokens **包含**缓存命中（缓存是它的子集）。usage_of 把
#:   acp 换算成后者：input_tokens = inputTokens + 缓存读 + 缓存写。不换算的话 run 表里两种
#:   智能体的 input_tokens 不是同一个口径，遥测后端按约定再减一次缓存，会得到负数（截成 0）。
_USAGE_KEYS: dict[str, tuple[str, ...]] = {
    "input_tokens": ("inputTokens",),
    "output_tokens": ("outputTokens",),
    "total_tokens": ("totalTokens",),
    "cache_read": ("cachedReadTokens", "cacheRead"),
    "cache_creation": ("cachedWriteTokens", "cacheCreation"),
    "thinking_tokens": ("thinkingTokens", "reasoningTokens"),
}


# ═══════════════════════════════════ session/update ═══════════════════════════════════


def translate_update(update: Any) -> list[EventIntent]:
    """一条 session/update 的 ``update`` 载荷 → 零或多条事件意图。形状不对不抛，产出为空。"""
    if not isinstance(update, Mapping):
        return []
    kind = update.get("sessionUpdate")
    if kind in ("agent_message_chunk", "agent_thought_chunk"):
        text = _text(update.get("content"))
        if not text:
            # 空块不产事件：CLI 的流里常有只带 signature 的占位块
            return []
        # ★ 思考走 thinking.delta，不是 message.delta：message.delta 的 data 只有 {text}，
        #   混进去会被拼进 assistant 的最终消息，用户看到一段与回答不连贯的自言自语
        event = (
            EventType.MESSAGE_DELTA if kind == "agent_message_chunk" else EventType.THINKING_DELTA
        )
        return [(event, {"text": text})]

    if kind == "tool_call":
        raw_input = update.get("rawInput")
        args = raw_input if raw_input is not None else {}
        return [
            (
                EventType.TOOL_STARTED,
                {
                    # toolCallId 原样带过去 —— 前端靠它把 started 与 completed 合并成一行
                    "call_id": _str(update.get("toolCallId")),
                    "name": _short_title(_str(update.get("title"))) or _str(update.get("kind")),
                    "args": args,
                    "args_preview": preview_args(args),
                },
            )
        ]

    if kind == "tool_call_update":
        event = _TOOL_TERMINAL.get(_str(update.get("status")))
        if event is None:
            # ★ 中间态里有一类不能丢：参数 / 名字的补全。CLI 在模型**刚开始**输出某个工具
            #   调用时就发 tool_call（只有「Edit」「Terminal」这样的标题，rawInput 为空），
            #   参数随后逐字段用 tool_call_update 补上（claude-agent-acp 的 refinement）。
            #   丢掉它们的话，工具行永远只显示「Edit{}」—— 看不出在改哪个文件。
            refined = _tool_refinement(update)
            return [(EventType.TOOL_UPDATED, refined)] if refined else []
        data: dict[str, Any] = {
            "call_id": _str(update.get("toolCallId")),
            "status": "error" if event is EventType.TOOL_FAILED else "success",
        }
        # ★ 缺席的字段要**省略**，不能写成空串：tool_call_update 只带变化的字段，而前端的
        #   upsertTool 是浅合并，空串会把 tool_call 时记下的工具名原地抹掉
        title = _short_title(_str(update.get("title")))
        if title:
            data["name"] = title
        refined = _tool_refinement(update)
        for key in ("args", "args_preview"):
            if key in refined:
                data[key] = refined[key]
        output = _tool_output(update.get("content"))
        if output:
            data["result"] = output
            data["result_preview"] = output[:_RESULT_PREVIEW]
            if event is EventType.TOOL_FAILED:
                # ★ 失败原因要进 error：前端的工具行读的是它，只放在 result 里就显示「无错误详情」
                data["error"] = output[:_RESULT_PREVIEW]
                denied = _auto_mode_denial(output)
                if denied is not None:
                    # 被 CLI 的 auto 模式拦下：不是工具坏了，是判断为有风险。用户无从批准，
                    # 要单独呈现（设计 D8-A）
                    data["error_kind"] = "auto_mode_denied"
                    data["denied_reason"] = denied
        return [(event, data)]

    if kind == "plan":
        # 全量快照语义与平台 todos.updated 一致；ACP 的 status 与平台 TodoStatus 同名同义
        entries = update.get("entries") if isinstance(update.get("entries"), list) else []
        todos = [
            {"content": _str(e.get("content")), "status": _str(e.get("status")) or "pending"}
            for e in entries
            if isinstance(e, Mapping)
        ]
        return [(EventType.TODOS_UPDATED, {"todos": todos})]

    if kind == "usage_update":
        # CLI 自管上下文（Claude Code 自带压缩）：平台不改写，只观测用量（上下文管理 §14）
        used, size = update.get("used"), update.get("size")
        if not isinstance(used, int) or not isinstance(size, int) or size <= 0:
            return []
        return [
            (
                EventType.CONTEXT_USAGE,
                {"tokens": used, "basis": "acp", "context_window": size, "effective_window": size},
            )
        ]

    if kind == "available_commands_update":
        # CLI 的斜杠命令清单：平台用不到，安静丢弃（不是未知类型）
        return []

    # ★ 不认识的类型不翻成 error：CLI / 适配器升一次版，不该让所有在跑的会话报错
    logger.warning("未翻译的 session/update 类型：%s", kind or "(空)")
    return []


# ═══════════════════════════════════ 一轮的结束 ═══════════════════════════════════


def translate_stop(stop_reason: str, usage: dict[str, Any] | None = None) -> list[EventIntent]:
    """ACP 的结束原因（+ 用量）→ 终止事件意图。用量排在终止事件之前（SSE 收到终止就关）。

    ★ 撞上限不是失败：max_tokens / max_turn_requests 映射成 run.finished 加 stop_reason。
      翻成 run.failed 的话，「做完了但被截断」与「真的出错了」在前端长得一模一样。
    """
    out: list[EventIntent] = []
    if usage is not None:
        out.append((EventType.USAGE_UPDATED, dict(usage)))

    if stop_reason == "cancelled":
        out.append((EventType.RUN_CANCELLED, {"stop_reason": stop_reason}))
        return out
    if stop_reason == "refusal":
        # 拒答不是故障：重试无用
        out.append(
            (
                EventType.RUN_FAILED,
                {
                    "error_kind": "model_refused",
                    "message": "模型拒绝作答",
                    "retryable": False,
                    "stop_reason": stop_reason,
                },
            )
        )
        return out
    if stop_reason not in STOP_REASONS:
        # 认不出的终了原因照样收尾 —— run 挂在 running 比标错更糟
        logger.warning("未知的 StopReason：%s", stop_reason)
    out.append(
        (
            EventType.RUN_FINISHED,
            {
                "total_tokens": usage["total_tokens"] if usage else 0,
                "text_len": 0,  # 由调用方按累计正文长度覆盖
                "stop_reason": stop_reason,
            },
        )
    )
    return out


def translate_crash(detail: str = "") -> list[EventIntent]:
    """agent 进程异常 → 可重试的 run.failed（崩溃是故障不是拒答，用户重试一次通常就好）。"""
    return [
        (
            EventType.RUN_FAILED,
            {
                "error_kind": "runtime_crashed",
                "message": detail or "CLI 进程异常退出",
                "retryable": True,
            },
        )
    ]


def usage_of(response: Any) -> dict[str, int] | None:
    """agent 的 prompt 响应里的 usage → usage.updated 的 data。没有或形状不对 → None。"""
    raw = response.get("usage") if isinstance(response, Mapping) else None
    if not isinstance(raw, Mapping):
        return None
    usage: dict[str, int] = {}
    for key, wires in _USAGE_KEYS.items():
        value = next((raw[w] for w in (*wires, key) if w in raw), 0)
        usage[key] = value if isinstance(value, int) and not isinstance(value, bool) else 0
    # 换算成「input 含缓存」的口径（见 _USAGE_KEYS 的说明）
    usage["input_tokens"] += usage["cache_read"] + usage["cache_creation"]
    return usage


def model_of(response: Any) -> str | None:
    """这一轮 CLI 实际用的模型（新 adapter 在 _meta.quota.model_usage 里报）。拿不到 → None。

    ★ 平台看不到 CLI 内部的单次模型调用，费用只能按这一轮的总用量 × 这个模型的单价算；
      没有模型名，遥测后端就匹配不上价格表。
    """
    meta = response.get("_meta") if isinstance(response, Mapping) else None
    quota = meta.get("quota") if isinstance(meta, Mapping) else None
    usages = quota.get("model_usage") if isinstance(quota, Mapping) else None
    if isinstance(usages, list):
        for item in usages:
            if isinstance(item, Mapping) and isinstance(item.get("model"), str) and item["model"]:
                return str(item["model"])
    return None


# ═══════════════════════════════════ 一轮的状态 ═══════════════════════════════════


class AcpV1Translator:
    """一轮一个实例：逐条映射之外，还持有这一轮的正文累积（Answer）。"""

    def __init__(self) -> None:
        self.answer = Answer()
        #: call_id → 上次发出的 (名字, 参数预览)。参数逐字段补全时，没有变化的不重复发
        self._tool_seen: dict[str, tuple[str, str]] = {}

    def update(self, params: Any) -> list[EventIntent]:
        """agent.update 的 ``update`` 字段（ACP session/update 的 params，原样）。"""
        if not isinstance(params, Mapping):
            return []
        out: list[EventIntent] = []
        for kind, data in translate_update(params.get("update")):
            if kind is EventType.MESSAGE_DELTA:
                # 正文累计 —— CLI 不发「完成」信号，message.completed 以一轮结束为界拼出
                self.answer.append(str(data.get("text", "")))
                data = {**data, "block": self.answer.index}
            elif kind is EventType.TOOL_STARTED:
                # 调工具 = 这一段说完了（与 native 同一条判据）
                self.answer.seal()
                self._tool_seen[str(data.get("call_id"))] = (
                    str(data.get("name") or ""),
                    str(data.get("args_preview") or ""),
                )
            elif kind is EventType.TOOL_UPDATED:
                call_id = str(data.get("call_id"))
                before = self._tool_seen.get(call_id, ("", ""))
                after = (
                    str(data.get("name") or before[0]),
                    str(data.get("args_preview") or before[1]),
                )
                if after == before:
                    continue
                self._tool_seen[call_id] = after
            out.append((kind, data))
        return out


# ═══════════════════════════════════ 内部 ═══════════════════════════════════


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


#: 工具名的显示上限：一行、80 字
_TITLE_MAX = 80


def _short_title(title: str) -> str:
    """工具名只取一行、最多 80 字。

    ★ claude-agent-acp 把 shell 命令**整段**当终端工具的标题（多行脚本也是），工具行的名字
      就成了一大段代码。命令本身已经在参数预览里，名字只需要能认出是哪一步。
    """
    lines = [x.strip() for x in title.strip().splitlines() if x.strip()]
    if not lines:
        return ""
    first = lines[0]
    more = len(lines) > 1 or len(first) > _TITLE_MAX
    return first[:_TITLE_MAX].rstrip() + ("…" if more else "")


def _tool_refinement(update: Mapping[str, Any]) -> dict[str, Any]:
    """tool_call_update 里的参数 / 名字补全 → tool.updated 的 data；什么都没补返回 {}。

    ★ 缺席的字段一律**省略**（同终态的处理）：前端 upsertTool 是浅合并，写成空值会把
      已经记下的名字 / 参数抹掉。
    ★ 没有 rawInput、只有 locations（文件位置）时，用路径当参数预览 —— 至少看得出动的是哪个文件。
    """
    data: dict[str, Any] = {}
    title = _short_title(_str(update.get("title")))
    if title:
        data["name"] = title
    raw = update.get("rawInput")
    if isinstance(raw, Mapping) and raw:
        data["args"] = dict(raw)
        data["args_preview"] = preview_args(dict(raw))
    else:
        paths = [
            _str(loc.get("path"))
            for loc in update.get("locations") or []
            if isinstance(loc, Mapping) and _str(loc.get("path"))
        ]
        if paths:
            data["args_preview"] = ", ".join(paths)
    if not data:
        return {}
    return {"call_id": _str(update.get("toolCallId")), **data}


def _text(block: Any) -> str:
    """ContentBlock → 文本。只取 text；image / audio 等本期不渲染。"""
    return _str(block.get("text")) if isinstance(block, Mapping) else ""


#: CLI 的 auto 模式拦截时的措辞（adapter 与 Claude Code 各有一种），方括号里是拦截类别
_AUTO_DENIAL = (
    re.compile(r"^Permission denied: \[([^\]]+)\]"),
    re.compile(r"auto mode classifier\. Reason: \[([^\]]+)\]"),
)


def _auto_mode_denial(output: str) -> str | None:
    """被 auto 模式拦截 → 拦截类别（如 "Unverifiable Deletion Target"）；否则 None。"""
    for pattern in _AUTO_DENIAL:
        match = pattern.search(output)
        if match:
            return match.group(1)
    return None


def _tool_output(content: Any) -> str:
    """tool_call_update 的 content → 一段文本（平台的工具结果只认字符串）。

    兼容两种形状：ACP 规范的 ``{"type": "content", "content": {<ContentBlock>}}``，
    以及直接给出 ContentBlock 的简写。diff / terminal 类内容不在这里渲染。
    """
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if not isinstance(item, Mapping):
            continue
        inner = item.get("content") if item.get("type") == "content" else item
        parts.append(_text(inner))
    return "".join(parts)
