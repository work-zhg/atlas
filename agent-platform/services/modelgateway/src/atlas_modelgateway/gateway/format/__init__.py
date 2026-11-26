"""格式（c4-model/modelgateway/code-gateway-format.html）：直通还是转换、读写器、连上游。

每个请求涉及两种格式：调用方用的（入口格式）和选中的账号用的（上游格式）。相同就直通，只换模型名、
按 ParamPlan 改几个参数，别的原样带过去。同一种格式下各家写法不一样的地方交给供应商方言。
★ 只有通用入口转换（Anthropic 格式转成账号有的 OpenAI / Google 格式）；Anthropic、OpenAI 两个入口
  只走同格式的线路，模型没有这种线路就报错。
★ 二进制文档（PDF 等）在通用入口只挑能承载文档的线路（Anthropic 原生或 Google inlineData）；
  若只有 OpenAI 线路则直接报 BEYOND_CAPABILITY，绝不静默丢弃。
★ 只按入口格式读回答：转换过的回答已经是入口格式。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from ...provider import Op, ProviderCatalog, ProviderDialect
from ...shared.kernel import (
    Entry,
    ErrorCategory,
    FailureKind,
    Format,
    GatewayError,
    ModelKind,
    ParamPlan,
    RequestSummary,
    Route,
    UpstreamFailure,
    Usage,
)
from ...shared.upstream import AddressGuard, UpstreamClient, UpstreamError
from .codecs import AnthropicCodec, FormatCodec, OpenAICodec, OutContext
from .convert import AnthropicToOpenAI, Converter, StreamTranscoder
from .convert_google import AnthropicToGoogle
from .sse import KEEPALIVE, SSEEvent, iter_sse

__all__ = [
    "KEEPALIVE",
    "AddressGuard",
    "FormatGateway",
    "FrameInfo",
    "Path",
    "PathChooser",
    "Piece",
    "UpstreamCall",
    "UpstreamClient",
    "UpstreamError",
]

#: 原样带给 Anthropic 格式上游的调用方请求头（新功能常常靠 beta 头打开）
_PASS_HEADERS = ("anthropic-beta", "anthropic-version")

_LABEL = {Format.ANTHROPIC: "Anthropic", Format.OPENAI: "OpenAI", Format.GOOGLE: "Google"}

_OPS = {
    (Format.ANTHROPIC, ModelKind.CHAT): Op.MESSAGES,
    (Format.OPENAI, ModelKind.CHAT): Op.CHAT,
    (Format.OPENAI, ModelKind.EMBEDDING): Op.EMBEDDINGS,
    (Format.OPENAI, ModelKind.RERANK): Op.RERANK,
}


@dataclass(frozen=True)
class FrameInfo:
    is_content: bool = False
    usage: Usage | None = None
    failure: UpstreamFailure | None = None
    stop_reason: str | None = None


@dataclass(frozen=True)
class Piece:
    """一段回答：data 已经是入口格式的字节（None = 不转给调用方），info 是它的要点。"""

    data: bytes | None
    info: FrameInfo


@dataclass(frozen=True)
class Path:
    upstream: Format
    base_url: str
    converter: Converter | None = None

    @property
    def direct(self) -> bool:
        return self.converter is None


#: 通用入口按顺序找转换目标：账号没有 Anthropic 格式地址时，先转 OpenAI，再转 Google
_CONVERT_TO: tuple[tuple[Format, Callable[[ProviderDialect], Converter]], ...] = (
    (Format.OPENAI, AnthropicToOpenAI),
    (Format.GOOGLE, AnthropicToGoogle),
)


class PathChooser:
    """入口格式和账号登记的某个地址格式相同就直通；通用入口没有同格式地址时选转换器。"""

    @staticmethod
    def _target(
        entry: Entry, route: Route, *, has_documents: bool = False
    ) -> tuple[Format, Callable[[ProviderDialect], Converter] | None] | None:
        endpoints = route.account.endpoints
        if endpoints.get(entry.format):
            return entry.format, None
        if entry.converts:
            for fmt, converter in _CONVERT_TO:
                if has_documents and fmt is Format.OPENAI:
                    continue
                if endpoints.get(fmt):
                    return fmt, converter
        return None

    @classmethod
    def serves(cls, entry: Entry, route: Route, *, has_documents: bool = False) -> bool:
        """这条线路能不能接这个入口的请求。"""
        return cls._target(entry, route, has_documents=has_documents) is not None

    @classmethod
    def choose(
        cls, entry: Entry, route: Route, dialect: ProviderDialect, *, has_documents: bool = False
    ) -> Path:
        target = cls._target(entry, route, has_documents=has_documents)
        if target is None:
            # 调用编排选线路前已经按 serves 筛过，正常走不到这里
            raise UpstreamError(
                UpstreamFailure(
                    FailureKind.OVERLOADED,
                    f"账号没有能接 {entry} 入口请求的地址"
                    f"（登记了 {', '.join(map(str, route.account.endpoints)) or '无'}）",
                )
            )
        fmt, converter = target
        endpoint = route.account.endpoints[fmt]
        return Path(fmt, endpoint, converter(dialect) if converter else None)


@dataclass
class UpstreamCall:
    """一次尝试的上游回答，逐段给出入口格式的回答。"""

    response: httpx.Response
    codec: FormatCodec
    dialect: ProviderDialect
    entry: Format
    path: Path
    ctx: OutContext
    dropped: list[str] = field(default_factory=list)

    async def pieces(self) -> AsyncIterator[Piece]:
        transcoder: StreamTranscoder | None = (
            self.path.converter.transcoder() if self.path.converter else None
        )
        async for event in iter_sse(self.response.aiter_lines()):
            outs = transcoder.feed(event) if transcoder else [event]
            for out in outs:
                yield self._piece(out)
        if transcoder:
            for out in transcoder.finish():
                yield self._piece(out)

    def _piece(self, event: SSEEvent) -> Piece:
        peek = self.codec.inspect(event, self.ctx)
        failure = self.dialect.classify(None, {}, peek.error) if peek.error is not None else None
        usage = self.dialect.usage(self.entry, peek.usage) if peek.usage else None
        data = None if peek.drop else self.codec.outgoing(event, self.ctx).encode()
        return Piece(data, FrameInfo(peek.is_content, usage, failure, peek.stop_reason))

    async def read(self) -> tuple[dict[str, Any], Usage]:
        """非流式：整个回答（入口格式）和用量。"""
        data = json.loads(await self.response.aread())
        if self.path.converter:
            data = self.path.converter.response(data)
        raw = self.codec.body_usage(data) or {}
        usage = self.dialect.usage(self.entry, raw)
        return self.codec.outgoing_body(data, self.ctx), usage

    async def aclose(self) -> None:
        await self.response.aclose()


class FormatGateway:
    def __init__(self, catalog: ProviderCatalog, client: UpstreamClient) -> None:
        self.catalog = catalog
        self.client = client
        self._codecs: dict[Format, FormatCodec] = {
            Format.ANTHROPIC: AnthropicCodec(),
            Format.OPENAI: OpenAICodec(),
        }

    def codec(self, fmt: Format) -> FormatCodec:
        return self._codecs[fmt]

    def read(
        self, entry: Entry, body: Mapping[str, Any], headers: Mapping[str, str], kind: ModelKind
    ) -> RequestSummary:
        return self.codec(entry.format).summarize(body, headers, kind)

    @staticmethod
    def routes_for(
        entry: Entry,
        model: str,
        routes: Sequence[Route],
        *,
        summary: RequestSummary | None = None,
    ) -> list[Route]:
        """筛这个入口能走的线路（直通缺格式或文档无承载线路直接报错）。"""
        usable = [r for r in routes if PathChooser.serves(entry, r)]
        if routes and not usable:
            raise GatewayError(
                ErrorCategory.FORMAT_UNSUPPORTED,
                f"模型 {model} 没有 {_LABEL[entry.format]} 格式的线路，这个接口不转换格式；"
                "对话请改用通用接口 /universal/v1/messages（按 Anthropic 格式传，网关按线路转换）",
                no_retry=True,
            )
        if summary is not None and summary.has_documents and entry is Entry.UNIVERSAL:
            doc_usable = [r for r in usable if PathChooser.serves(entry, r, has_documents=True)]
            if usable and not doc_usable:
                raise GatewayError(
                    ErrorCategory.BEYOND_CAPABILITY,
                    f"模型 {model} 当前的线路是 OpenAI 格式，不支持直接读取 PDF 等二进制文档",
                    no_retry=True,
                )
            return doc_usable
        return usable

    async def open(
        self,
        entry: Entry,
        kind: ModelKind,
        body: Mapping[str, Any],
        headers: Mapping[str, str],
        route: Route,
        plan: ParamPlan,
        *,
        model: str,
        stream: bool,
        read_timeout_s: float | None,
        has_documents: bool = False,
    ) -> UpstreamCall:
        """发出一次尝试。拿到 2xx 响应头就返回；连不上、超时、错误状态码抛 UpstreamError。"""
        dialect = self.catalog.dialect(route.account.provider)
        path = PathChooser.choose(entry, route, dialect, has_documents=has_documents)
        codec = self.codec(entry.format)
        payload = codec.apply(body, plan)
        dropped: list[str] = []
        if path.converter:
            payload, dropped = path.converter.request(payload, plan)
            # 转换后按 ParamPlan 再写一遍上游格式的参数
            # （入口格式表达不了的，比如 OpenAI 格式里的思考）
            if path.upstream in self._codecs:
                payload = self.codec(path.upstream).apply(payload, plan)
        payload = dialect.adjust(path.upstream, payload, plan)
        op = _OPS.get((path.upstream, kind))
        if path.upstream is Format.GOOGLE and kind is ModelKind.CHAT:
            op = Op.STREAM_GENERATE if stream else Op.GENERATE
        if op is None:
            raise UpstreamError(
                UpstreamFailure(FailureKind.OVERLOADED, f"{path.upstream} 格式的账号做不了{kind}")
            )
        url = dialect.url(path.upstream, path.base_url, op, plan.upstream_model)
        send_headers = {
            "content-type": "application/json",
            **dialect.auth(path.upstream, route.account.secret),
        }
        if path.direct and path.upstream is Format.ANTHROPIC:
            lower = {k.lower(): v for k, v in headers.items()}
            for name in _PASS_HEADERS:
                if lower.get(name):
                    send_headers[name] = lower[name]
            send_headers.setdefault("anthropic-version", "2023-06-01")
        if stream:
            send_headers["accept"] = "text/event-stream"
        response = await self.client.send(
            url,
            headers=send_headers,
            body=payload,
            egress=route.account.egress,
            stream=stream,
            read_timeout_s=read_timeout_s,
        )
        if response.status_code >= 400:
            raw = await response.aread()
            await response.aclose()
            raise UpstreamError(dialect.classify(response.status_code, response.headers, raw))
        ctx = OutContext(
            model=model,
            stamp=plan.stamp,
            caller_wants_usage=bool((body.get("stream_options") or {}).get("include_usage"))
            or entry.format is not Format.OPENAI,
        )
        return UpstreamCall(response, codec, dialect, entry.format, path, ctx, dropped)

    async def count_tokens(
        self, body: Mapping[str, Any], headers: Mapping[str, str], route: Route, plan: ParamPlan
    ) -> dict[str, Any]:
        dialect = self.catalog.dialect(route.account.provider)
        payload = self.codec(Format.ANTHROPIC).apply(body, plan)
        for key in ("stream", "max_tokens", "temperature", "metadata", "stop_sequences"):
            payload.pop(key, None)
        url = dialect.url(
            Format.ANTHROPIC, route.account.endpoints[Format.ANTHROPIC], Op.COUNT_TOKENS
        )
        send_headers = {
            "content-type": "application/json",
            **dialect.auth(Format.ANTHROPIC, route.account.secret),
        }
        send_headers.setdefault("anthropic-version", "2023-06-01")
        response = await self.client.send(
            url,
            headers=send_headers,
            body=payload,
            egress=route.account.egress,
            stream=False,
            read_timeout_s=30.0,
        )
        if response.status_code >= 400:
            raise UpstreamError(
                dialect.classify(response.status_code, response.headers, response.content)
            )
        return response.json()

    def error_body(self, entry: Entry, err: GatewayError) -> bytes:
        return json.dumps(self.codec(entry.format).error_body(err), ensure_ascii=False).encode()

    def error_event(self, entry: Entry, err: GatewayError) -> bytes:
        return self.codec(entry.format).error_event(err).encode()
