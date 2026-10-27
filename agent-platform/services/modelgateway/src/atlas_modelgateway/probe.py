"""能力探测（模型管理方案设计 §4.4）：经「模型网关 → Higress → 供应商」的完整链路跑一组小请求，
得出建议写进目录的属性。

    对话   两种协议各跑：基本对话、流式（含用量回报）、工具调用、并行工具调用、多轮工具调用
           （开着思考）、采样参数、提示词缓存、图片输入；Anthropic 另测自适应思考、预算式思考、
           关闭思考、档位
    嵌入   基本调用，得出向量维度
    重排   基本调用

★ 只探测供应商本身不够：协议转换会改变结果。已知的例子是思考内容在 Anthropic 转 OpenAI 时丢失，
  开着思考的多轮工具调用会被供应商拒绝 —— 所以「协议能用」以多轮工具调用通过为准。
★ 探测结果不直接改目录：供应商偶尔抖动，一次失败不代表不支持。管理员确认后才写进去
  （registry.apply_probe）。
★ 会花真实的 token（几十次小请求），只在管理员手动触发时跑。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from .catalog import Capabilities, protocols_for
from .db.models import GwModel
from .settings import GatewaySettings
from .upstream import PATHS, Upstream, describe_error
from .usage import Meter

logger = logging.getLogger(__name__)

__all__ = ["ProbeOutcome", "run_probe"]

#: 8×8 的红色 PNG（图片输入探测）
_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAEklEQVR4nGP4z8CAFWEXHbQSACj/"
    "P8Hfd98WAAAAAElFTkSuQmCC"
)
_WEATHER = {
    "name": "get_weather",
    "description": "查询一个城市现在的天气",
    "parameters": {
        "type": "object",
        "properties": {"city": {"type": "string", "description": "城市名"}},
        "required": ["city"],
    },
}
#: 提示词缓存探测用的长前缀（各家最小可缓存长度在 1K–4K token 之间）
_LONG_PREFIX = "你是一个严谨的助手。以下是背景资料，回答时不必复述。\n" + (
    "资料：模型网关负责认人、判权、计量和转发，供应商的凭据只在上游网关里。\n" * 400
)


@dataclass
class Check:
    check: str
    protocol: str
    ok: bool
    detail: str = ""


@dataclass
class ProbeOutcome:
    results: list[Check] = field(default_factory=list)
    suggested: dict[str, Any] = field(default_factory=dict)
    differences: dict[str, Any] = field(default_factory=dict)

    def passed(self, check: str, protocol: str) -> bool:
        return any(r.ok for r in self.results if r.check == check and r.protocol == protocol)


class _Client:
    def __init__(self, settings: GatewaySettings, http: httpx.AsyncClient) -> None:
        self.upstream = Upstream(settings)
        self.http = http

    def _headers(self, protocol: str) -> dict[str, str]:
        headers = {**self.upstream.credential_headers(), "Content-Type": "application/json"}
        if protocol == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
        return headers

    async def post(self, kind: str, protocol: str, body: dict[str, Any]) -> httpx.Response:
        return await self.http.post(
            self.upstream.url(PATHS[(kind, protocol)]), json=body, headers=self._headers(protocol)
        )

    async def stream(self, protocol: str, body: dict[str, Any]) -> tuple[int, Any, str]:
        """流式对话 → (状态码, 用量, 错误说明)。"""
        meter = Meter("messages" if protocol == "anthropic" else "chat", limit=4 * 1024 * 1024)
        async with self.http.stream(
            "POST",
            self.upstream.url(PATHS[("chat", protocol)]),
            json=body,
            headers=self._headers(protocol),
        ) as r:
            if r.status_code >= 400:
                return r.status_code, None, _brief(await r.aread())
            meter.start(r.headers.get("content-type", ""))
            async for chunk in r.aiter_raw():
                meter.feed(chunk)
        return r.status_code, meter.result(), ""


def _brief(content: bytes) -> str:
    return content.decode("utf-8", "replace")[:300]


def _detail(r: httpx.Response) -> str:
    return f"HTTP {r.status_code}：{_brief(r.content)}"


# ───────────────────────────── Anthropic Messages


def _anthropic_tool_calls(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [b for b in body.get("content") or [] if b.get("type") == "tool_use"]


def _anthropic_tool() -> dict[str, Any]:
    return {
        "name": _WEATHER["name"],
        "description": _WEATHER["description"],
        "input_schema": _WEATHER["parameters"],
    }


async def _probe_anthropic(c: _Client, model: str, out: ProbeOutcome) -> None:
    base = {"model": model, "max_tokens": 64}
    ask = [{"role": "user", "content": "只回复两个字：好的"}]

    async def check(name: str, body: dict[str, Any], *, ok: Any = None) -> dict[str, Any] | None:
        try:
            r = await c.post("chat", "anthropic", body)
        except httpx.HTTPError as exc:
            out.results.append(Check(name, "anthropic", False, describe_error(exc)))
            return None
        if r.status_code >= 400:
            out.results.append(Check(name, "anthropic", False, _detail(r)))
            return None
        data = r.json()
        good = ok(data) if ok else True
        out.results.append(Check(name, "anthropic", bool(good), "" if good else _brief(r.content)))
        return data if good else None

    if await check("basic", {**base, "messages": ask}) is None:
        return  # 基本对话都不通，其余不用测了
    try:
        status, usage, error = await c.stream(
            "anthropic", {**base, "messages": ask, "stream": True}
        )
        out.results.append(Check("stream", "anthropic", status < 400, error))
        out.results.append(
            Check(
                "stream_usage",
                "anthropic",
                bool(usage and usage.reported),
                "" if usage and usage.reported else "流式响应里没有用量",
            )
        )
    except httpx.HTTPError as exc:
        out.results.append(Check("stream", "anthropic", False, describe_error(exc)))

    tools = {"tools": [_anthropic_tool()], "tool_choice": {"type": "any"}}
    first = await check(
        "tools",
        {
            **base,
            "max_tokens": 256,
            **tools,
            "messages": [{"role": "user", "content": "北京现在天气怎么样？"}],
        },
        ok=lambda d: bool(_anthropic_tool_calls(d)),
    )
    await check(
        "parallel_tools",
        {
            **base,
            "max_tokens": 512,
            "tools": [_anthropic_tool()],
            "messages": [
                {"role": "user", "content": "同时查一下北京和上海现在的天气，两个城市一起查。"}
            ],
        },
        ok=lambda d: len(_anthropic_tool_calls(d)) >= 2,
    )

    thinking: dict[str, Any] | None = None
    if (
        await check(
            "thinking_adaptive",
            {**base, "max_tokens": 2048, "thinking": {"type": "adaptive"}, "messages": ask},
        )
        is not None
    ):
        thinking = {"type": "adaptive"}
    if (
        await check(
            "thinking_budget",
            {
                **base,
                "max_tokens": 2048,
                "thinking": {"type": "enabled", "budget_tokens": 1024},
                "messages": ask,
            },
        )
        is not None
        and thinking is None
    ):
        thinking = {"type": "enabled", "budget_tokens": 1024}
    await check("thinking_disabled", {**base, "thinking": {"type": "disabled"}, "messages": ask})
    await check("effort", {**base, "output_config": {"effort": "low"}, "messages": ask})
    await check("temperature", {**base, "temperature": 0.2, "messages": ask})

    # 多轮工具调用：把工具结果交回去，开着思考（如果有）——「协议能用」以它为准
    if first is not None:
        call = _anthropic_tool_calls(first)[0]
        turn = {
            **base,
            "max_tokens": 2048 if thinking else 256,
            "tools": [_anthropic_tool()],
            "messages": [
                {"role": "user", "content": "北京现在天气怎么样？"},
                {"role": "assistant", "content": first.get("content") or []},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": call["id"],
                            "content": "北京：晴，18 度",
                        }
                    ],
                },
            ],
        }
        if thinking:
            # 第一轮也要开着思考才有思考块可交回：重新带思考问一次
            again = await c.post(
                "chat",
                "anthropic",
                {
                    **base,
                    "max_tokens": 2048,
                    "thinking": thinking,
                    "tools": [_anthropic_tool()],
                    "messages": [
                        {"role": "user", "content": "北京现在天气怎么样？请调用工具查询。"}
                    ],
                },
            )
            if again.status_code < 400 and _anthropic_tool_calls(again.json()):
                data = again.json()
                call = _anthropic_tool_calls(data)[0]
                turn["messages"][1] = {"role": "assistant", "content": data.get("content") or []}
                turn["messages"][2]["content"][0]["tool_use_id"] = call["id"]
                turn["thinking"] = thinking
        await check("multi_turn_tools", turn)

    cached = {
        **base,
        "system": [{"type": "text", "text": _LONG_PREFIX, "cache_control": {"type": "ephemeral"}}],
        "messages": ask,
    }
    hit = False
    detail = ""
    for _ in range(2):
        try:
            r = await c.post("chat", "anthropic", cached)
        except httpx.HTTPError as exc:
            detail = describe_error(exc)
            break
        if r.status_code >= 400:
            detail = _detail(r)
            break
        usage = r.json().get("usage") or {}
        hit = int(usage.get("cache_read_input_tokens") or 0) > 0
    out.results.append(
        Check("prompt_cache", "anthropic", hit, detail or ("" if hit else "第二次请求没有命中缓存"))
    )

    await check(
        "vision",
        {
            **base,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": "image/png", "data": _PNG},
                        },
                        {"type": "text", "text": "这张图是什么颜色？只回答颜色。"},
                    ],
                }
            ],
        },
    )


# ───────────────────────────── OpenAI Chat Completions


def _openai_tool_calls(body: dict[str, Any]) -> list[dict[str, Any]]:
    choices = body.get("choices") or [{}]
    return list((choices[0].get("message") or {}).get("tool_calls") or [])


async def _probe_openai(c: _Client, model: str, out: ProbeOutcome) -> None:
    base = {"model": model, "max_tokens": 64}
    ask = [{"role": "user", "content": "只回复两个字：好的"}]
    tool = {"type": "function", "function": _WEATHER}

    async def check(name: str, body: dict[str, Any], *, ok: Any = None) -> dict[str, Any] | None:
        try:
            r = await c.post("chat", "openai", body)
        except httpx.HTTPError as exc:
            out.results.append(Check(name, "openai", False, describe_error(exc)))
            return None
        if r.status_code >= 400:
            out.results.append(Check(name, "openai", False, _detail(r)))
            return None
        data = r.json()
        good = ok(data) if ok else True
        out.results.append(Check(name, "openai", bool(good), "" if good else _brief(r.content)))
        return data if good else None

    if await check("basic", {**base, "messages": ask}) is None:
        return
    try:
        status, usage, error = await c.stream(
            "openai",
            {**base, "messages": ask, "stream": True, "stream_options": {"include_usage": True}},
        )
        out.results.append(Check("stream", "openai", status < 400, error))
        out.results.append(
            Check(
                "stream_usage",
                "openai",
                bool(usage and usage.reported),
                "" if usage and usage.reported else "流式响应里没有用量",
            )
        )
    except httpx.HTTPError as exc:
        out.results.append(Check("stream", "openai", False, describe_error(exc)))

    question = [{"role": "user", "content": "北京现在天气怎么样？请调用工具查询。"}]
    first = await check(
        "tools",
        {
            **base,
            "max_tokens": 256,
            "tools": [tool],
            "tool_choice": "required",
            "messages": question,
        },
        ok=lambda d: bool(_openai_tool_calls(d)),
    )
    await check(
        "parallel_tools",
        {
            **base,
            "max_tokens": 512,
            "tools": [tool],
            "messages": [
                {"role": "user", "content": "同时查一下北京和上海现在的天气，两个城市一起查。"}
            ],
        },
        ok=lambda d: len(_openai_tool_calls(d)) >= 2,
    )
    if first is not None:
        message = (first.get("choices") or [{}])[0].get("message") or {}
        call = _openai_tool_calls(first)[0]
        await check(
            "multi_turn_tools",
            {
                **base,
                "max_tokens": 256,
                "tools": [tool],
                "messages": [
                    *question,
                    # ★ 原样交回（含 reasoning_content 等供应商字段）：转换丢字段会在这里暴露
                    message,
                    {"role": "tool", "tool_call_id": call.get("id"), "content": "北京：晴，18 度"},
                ],
            },
        )
    await check("temperature", {**base, "temperature": 0.2, "messages": ask})
    await check(
        "structured_output",
        {
            **base,
            "max_tokens": 128,
            "messages": [{"role": "user", "content": "给出一个城市和它的国家"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "city",
                    "schema": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}, "country": {"type": "string"}},
                        "required": ["city", "country"],
                    },
                },
            },
        },
        ok=lambda d: _is_json((d.get("choices") or [{}])[0].get("message", {}).get("content")),
    )
    hit = False
    detail = ""
    for _ in range(2):
        try:
            r = await c.post(
                "chat",
                "openai",
                {**base, "messages": [{"role": "system", "content": _LONG_PREFIX}, *ask]},
            )
        except httpx.HTTPError as exc:
            detail = describe_error(exc)
            break
        if r.status_code >= 400:
            detail = _detail(r)
            break
        u = r.json().get("usage") or {}
        cached = int(
            (u.get("prompt_tokens_details") or {}).get("cached_tokens")
            or u.get("prompt_cache_hit_tokens")
            or 0
        )
        hit = cached > 0
    out.results.append(
        Check("prompt_cache", "openai", hit, detail or ("" if hit else "第二次请求没有命中缓存"))
    )
    await check(
        "vision",
        {
            **base,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{_PNG}"},
                        },
                        {"type": "text", "text": "这张图是什么颜色？只回答颜色。"},
                    ],
                }
            ],
        },
    )


def _is_json(text: Any) -> bool:
    try:
        return isinstance(json.loads(str(text)), dict)
    except ValueError:
        return False


# ───────────────────────────── 嵌入、重排


async def _probe_embedding(c: _Client, model: str, out: ProbeOutcome) -> int | None:
    try:
        r = await c.post("embedding", "openai", {"model": model, "input": ["你好", "世界"]})
    except httpx.HTTPError as exc:
        out.results.append(Check("basic", "openai", False, describe_error(exc)))
        return None
    if r.status_code >= 400:
        out.results.append(Check("basic", "openai", False, _detail(r)))
        return None
    data = r.json().get("data") or []
    dims = len(data[0].get("embedding") or []) if data else 0
    out.results.append(Check("basic", "openai", dims > 0, f"维度 {dims}"))
    out.results.append(Check("usage", "openai", bool(r.json().get("usage") or {}), ""))
    return dims or None


async def _probe_rerank(c: _Client, model: str, out: ProbeOutcome) -> None:
    body = {"model": model, "query": "天气", "documents": ["今天晴", "股票上涨"], "top_n": 2}
    try:
        r = await c.post("rerank", "rerank", body)
    except httpx.HTTPError as exc:
        out.results.append(Check("basic", "rerank", False, describe_error(exc)))
        return
    ok = r.status_code < 400 and bool(r.json().get("results"))
    out.results.append(Check("basic", "rerank", ok, "" if ok else _detail(r)))


# ───────────────────────────── 汇总


def _suggest_chat(row: GwModel, out: ProbeOutcome) -> dict[str, Any]:
    current = Capabilities.model_validate(row.capabilities or {})

    def usable(protocol: str) -> bool:
        need = ["basic", "stream"] + (["multi_turn_tools"] if out.passed("tools", protocol) else [])
        return all(out.passed(c, protocol) for c in need)

    candidates = [*(row.protocols or []), "anthropic", "openai"]
    protocols = [p for p in dict.fromkeys(candidates) if p in protocols_for("chat") and usable(p)]
    preferred = protocols[0] if protocols else "anthropic"

    def any_(check: str) -> bool:
        return any(out.passed(check, p) for p in protocols)

    thinking = out.passed("thinking_adaptive", "anthropic") or out.passed(
        "thinking_budget", "anthropic"
    )
    caps = Capabilities(
        tools=any_("tools"),
        parallel_tools=any_("parallel_tools"),
        structured_output=any_("structured_output"),
        vision=any_("vision"),
        thinking=thinking or current.thinking,
        adaptive_thinking=out.passed("thinking_adaptive", "anthropic"),
        thinking_always_on=thinking and not out.passed("thinking_disabled", "anthropic"),
        effort=out.passed("effort", "anthropic"),
        thinking_off_max_effort=current.thinking_off_max_effort,
        temperature=out.passed("temperature", preferred),
        prompt_cache=out.passed("prompt_cache", preferred),
        min_cacheable_tokens=current.min_cacheable_tokens,
        stream_usage=out.passed("stream_usage", preferred),
    )
    modalities = ["text", "image"] if caps.vision else ["text"]
    return {
        "protocols": protocols,
        "capabilities": caps.model_dump(),
        "input_modalities": modalities,
    }


async def run_probe(settings: GatewaySettings, row: GwModel) -> ProbeOutcome:
    """跑一遍探测。网关凭据没配置时直接抛 RuntimeError（由调用方翻译）。"""
    out = ProbeOutcome()
    timeout = httpx.Timeout(settings.probe_timeout_s, connect=settings.connect_timeout_s)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as http:
        c = _Client(settings, http)
        c.upstream.credential_headers()  # 没配置凭据：在发任何请求之前失败
        if row.kind == "chat":
            await _probe_anthropic(c, row.upstream_model, out)
            await _probe_openai(c, row.upstream_model, out)
            out.suggested = _suggest_chat(row, out)
        elif row.kind == "embedding":
            dims = await _probe_embedding(c, row.upstream_model, out)
            out.suggested = {"protocols": ["openai"] if out.passed("basic", "openai") else []}
            if dims:
                out.suggested["dimensions"] = dims
        else:
            await _probe_rerank(c, row.upstream_model, out)
            out.suggested = {"protocols": ["rerank"] if out.passed("basic", "rerank") else []}
    for key, value in out.suggested.items():
        before = getattr(row, key)
        if key == "capabilities":
            diff = {k: [before.get(k), v] for k, v in value.items() if (before or {}).get(k) != v}
            if diff:
                out.differences[key] = diff
        elif before != value:
            out.differences[key] = [before, value]
    return out
