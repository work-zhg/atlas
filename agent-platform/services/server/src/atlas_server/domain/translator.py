"""模型流 → TraceEvent 的原始素材提取（文档 §4.2）。

P1 只处理"纯文本单轮"。接入 kernel 后，这里扩展为
LangGraph stream → 工具/子智能体/待办事件的完整映射；
提取逻辑集中在此，runner 不直接碰 LangChain 的数据结构。

LangChain 的 chunk.content 有两种形态，必须都处理：
  · str            —— 简单文本增量
  · list[dict]     —— content blocks（thinking / text / tool_use 混排）
"""

from __future__ import annotations

from typing import Any


def extract_text(content: Any) -> str:
    """从 chunk.content 里取出可见文本增量。thinking block 不算可见文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
        return "".join(parts)
    return ""


def has_thinking(content: Any) -> bool:
    """该 chunk 是否携带思考内容。

    注意：经 litellm 网关时 thinking 文本恒为空（display 参数不透传，实测），
    所以这里只能判断"是否思考过"，拿不到推理文本。
    """
    if isinstance(content, list):
        return any(
            isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking")
            for b in content
        )
    return False


def normalize_usage(usage: Any) -> dict[str, int]:
    """把 LangChain 的 usage_metadata 规整成 usage.updated 事件的 data。

    thinking_tokens 来自 output_token_details（网关实测可用），缺失时为 0。
    """
    if not isinstance(usage, dict):
        return {}

    input_details = usage.get("input_token_details") or {}
    output_details = usage.get("output_token_details") or {}

    def _int(source: Any, key: str) -> int:
        value = source.get(key) if isinstance(source, dict) else None
        return int(value) if isinstance(value, int) else 0

    return {
        "input_tokens": _int(usage, "input_tokens"),
        "output_tokens": _int(usage, "output_tokens"),
        "total_tokens": _int(usage, "total_tokens"),
        "cache_read": _int(input_details, "cache_read"),
        "cache_creation": _int(input_details, "cache_creation"),
        "thinking_tokens": _int(output_details, "reasoning"),
    }


# ---------------------------------------------------------------------------
# LangGraph updates 流 → TraceEvent 素材（P4）
#
# 形状来自对 kernel agent 图的实测，不是照文档猜的：
#   node=model → {"messages": [AIMessage]}，AIMessage.tool_calls 即工具调用
#   node=tools → {"todos": [...], "messages": [ToolMessage]}
#             或 {"files": {path: {...}}, "messages": [ToolMessage]}
#   ToolMessage 带 .name / .status("success"|"error") / .tool_call_id
# ---------------------------------------------------------------------------


def tool_calls_from(message: Any) -> list[dict[str, Any]]:
    """AIMessage 里发起的工具调用 → tool.started 的 data。"""
    calls = getattr(message, "tool_calls", None)
    if not calls:
        return []
    out: list[dict[str, Any]] = []
    for call in calls:
        args = call.get("args") or {}
        out.append(
            {
                "call_id": call.get("id", ""),
                "name": call.get("name", ""),
                "args": args,
                # 收起态只显示摘要，避免把整个 SQL / 文件内容塞进列表项
                "args_preview": preview_args(args),
            }
        )
    return out


def tool_result_from(message: Any) -> dict[str, Any] | None:
    """ToolMessage → tool.completed / tool.failed 的 data。"""
    tool_call_id = getattr(message, "tool_call_id", None)
    if tool_call_id is None:
        return None
    content = getattr(message, "content", "")
    text = content if isinstance(content, str) else str(content)
    return {
        "call_id": tool_call_id,
        "name": getattr(message, "name", "") or "",
        "status": getattr(message, "status", "success") or "success",
        "result": text,
        "result_preview": text[:200],
    }


def files_delta(files: Any, seen: dict[str, str]) -> list[dict[str, Any]]:
    """虚拟文件系统快照 → 仅**变化过**的文件（file.written 的 data）。

    kernel 每次给的是整张表，直接全发会让 Inspector 反复刷同样的行。
    用内容摘要比对，只报新增与修改。
    """
    if not isinstance(files, dict):
        return []
    changed: list[dict[str, Any]] = []
    for path, meta in files.items():
        content = meta.get("content", "") if isinstance(meta, dict) else str(meta)
        digest = f"{len(content)}:{hash(content)}"
        if seen.get(path) == digest:
            continue
        seen[path] = digest
        changed.append(
            {
                "path": path,
                "size_bytes": len(content.encode("utf-8")),
                "digest": digest,
            }
        )
    return changed


def preview_args(args: Any, limit: int = 120) -> str:
    """工具入参的收起态摘要。

    ★ 转正为公开名（原 `_preview`）：acp 的 translate 也要用它 —— 工具行的
      摘要形态必须与 native 一致，否则前端会看到两种长相的同一种东西。
    """
    if isinstance(args, dict):
        parts = [f"{k}={_short(v)}" for k, v in args.items()]
        text = " ".join(parts)
    else:
        text = str(args)
    return text[:limit]


def _short(value: Any, limit: int = 60) -> str:
    text = value if isinstance(value, str) else str(value)
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"
