"""调用编排（c4-model/modelgateway/code-gateway-invocation.html）：一次调用的流程，自己不含规则。

    读共同信息 → 准入 → 查能力 → 按入口筛线路 → 选线路 → 按线路算参数 → 发出
    → 等第一段内容（提交点）→ 读一段写一段 → 记用量、累加花费

★ 提交点是「第一段内容到了」，不是上游返回 200：之前出的错（包括 200 之后流里来的过载）照样换线路，
  调用方只看到保活注释；之后出的错只能在流里报错结束。
★ 读一段写一段，不经中继、不放队列：调用方读得慢写就等，读上游也跟着慢；调用方断开就关上游。
★ 只限「等第一段内容」和「两段之间」，不限总时长。
★ 网关已经换过线路仍然失败时，告诉 SDK 不要再重试（官方 SDK 认 x-should-retry: false）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from atlas_tenancy import TenantScope

from ..db.types import uuid7
from .admission import Admission, Grant
from .capability import CapabilityGate
from .format import KEEPALIVE, FormatGateway, Piece, UpstreamCall, UpstreamError
from .kernel import (
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
from .metering import CallRecord, Metering, UsageEstimator
from .routing import RouteSelector

__all__ = ["Answer", "Attempt", "Call", "ErrorMapper", "Invocation", "Timeouts"]

logger = logging.getLogger(__name__)

MSG_BUSY = "模型繁忙，请稍后重试或换个模型"
MSG_NO_ROUTE = "这个模型暂时用不了，请换个模型"
MSG_UPSTREAM = "系统忙不过来了，请稍后再试"
MSG_INTERRUPTED = "回答中断了，请重试"


@dataclass(frozen=True)
class Timeouts:
    first_content_s: float = 600.0
    idle_s: float = 300.0
    keepalive_s: float = 15.0
    max_attempts: int = 4


@dataclass
class Attempt:
    route: Route
    plan: ParamPlan
    started: float = field(default_factory=time.monotonic)
    path: str = ""
    outcome: str = ""
    message: str = ""
    first_content_ms: int | None = None
    dropped: list[str] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        return {
            "route": str(self.route.uuid),
            "account": str(self.route.account.uuid),
            "provider": self.route.account.provider,
            "upstream_model": self.route.upstream_model,
            "path": self.path,
            "outcome": self.outcome,
            "message": self.message[:300],
            "ms": int((time.monotonic() - self.started) * 1000),
            "first_content_ms": self.first_content_ms,
        }


@dataclass
class Call:
    """一次调用从进门到结束的全部事实；计量按它算费用、写调用记录。"""

    entry: Entry
    kind: ModelKind
    call_uuid: UUID = field(default_factory=uuid7)
    started: float = field(default_factory=time.monotonic)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    summary: RequestSummary | None = None
    grant: Grant | None = None
    #: 这个入口能走的线路（不转换的入口只留同格式的）
    routes: list[Route] = field(default_factory=list)
    attempts: list[Attempt] = field(default_factory=list)
    committed: bool = False
    usage: Usage = field(default_factory=Usage)
    output_chars: int = 0
    first_content_ms: int | None = None
    status: str = "ok"
    error: GatewayError | None = None

    def attempt(self, route: Route, plan: ParamPlan) -> Attempt:
        attempt = Attempt(route, plan)
        self.attempts.append(attempt)
        return attempt

    def commit(self, attempt: Attempt) -> None:
        self.committed = True
        attempt.first_content_ms = int((time.monotonic() - attempt.started) * 1000)
        self.first_content_ms = int((time.monotonic() - self.started) * 1000)

    @property
    def session_key(self) -> str:
        if self.summary and self.summary.session_key:
            return self.summary.session_key
        return str(self.grant.key.key_uuid) if self.grant else ""


@dataclass
class Answer:
    """给入口的结果：流式时是逐段的字节，非流式时是整个回答体。"""

    status: int = 200
    body: bytes | None = None
    stream: AsyncIterator[bytes] | None = None
    headers: dict[str, str] = field(default_factory=dict)


class ErrorMapper:
    """把各种失败归成统一的类别和给用户的话。"""

    @staticmethod
    def from_failures(call: Call, *, routes_exist: bool) -> GatewayError:
        failures = [a for a in call.attempts if a.outcome and a.outcome != "ok"]
        model = call.summary.model if call.summary else ""
        if not routes_exist:
            return GatewayError(
                ErrorCategory.NO_ROUTE,
                MSG_NO_ROUTE,
                detail=f"模型 {model} 没有配置线路",
                no_retry=True,
            )
        if not failures:
            return GatewayError(
                ErrorCategory.NO_ROUTE,
                MSG_NO_ROUTE,
                detail="线路的账号都停用或异常（余额不足、密钥失效）",
                no_retry=True,
            )
        kinds = {a.outcome for a in failures}
        last = failures[-1]
        if kinds <= {FailureKind.RATE_LIMITED.value}:
            return GatewayError(
                ErrorCategory.MODEL_BUSY, MSG_BUSY, no_retry=True, detail=last.message
            )
        if kinds <= {FailureKind.NO_BALANCE.value, FailureKind.AUTH_FAILED.value}:
            return GatewayError(
                ErrorCategory.NO_ROUTE, MSG_NO_ROUTE, no_retry=True, detail=last.message
            )
        if last.outcome == "timeout":
            return GatewayError(
                ErrorCategory.UPSTREAM_TIMEOUT, MSG_UPSTREAM, no_retry=True, detail=last.message
            )
        return GatewayError(
            ErrorCategory.UPSTREAM_ERROR, MSG_UPSTREAM, no_retry=True, detail=last.message
        )

    @staticmethod
    def bad_request(failure: UpstreamFailure) -> GatewayError:
        _, message = _upstream_message(failure)
        return GatewayError(
            ErrorCategory.BAD_REQUEST,
            message or "请求有误（上游拒绝）",
            upstream_body=failure.body,
            detail=failure.message,
        )

    @staticmethod
    def interrupted(detail: str) -> GatewayError:
        return GatewayError(
            ErrorCategory.INTERRUPTED, MSG_INTERRUPTED, no_retry=True, detail=detail
        )


def _upstream_message(failure: UpstreamFailure) -> tuple[str, str]:
    from ..provider.dialect import error_fields

    kind, _, message = error_fields(failure.body)
    return kind, message


def _outcome(failure: UpstreamFailure) -> str:
    return "timeout" if failure.timeout else failure.kind.value


class Invocation:
    def __init__(
        self,
        admission: Admission,
        capability: CapabilityGate,
        routing: RouteSelector,
        fmt: FormatGateway,
        metering: Metering,
        timeouts: Timeouts | None = None,
    ) -> None:
        self.admission = admission
        self.capability = capability
        self.routing = routing
        self.fmt = fmt
        self.metering = metering
        self.t = timeouts or Timeouts()

    # ── 入口 ───────────────────────────────────────────────────────────

    async def call(
        self,
        entry: Entry,
        kind: ModelKind,
        secret: str | None,
        body: Mapping[str, Any],
        headers: Mapping[str, str],
    ) -> Answer:
        """跑一次调用。流式时返回逐段的字节（第一段之前的错误直接抛 GatewayError）。"""
        call = Call(entry, kind)
        call.summary = self.fmt.read(entry, body, headers, kind)
        try:
            call.grant = await self.admission.admit(secret, call.summary.model, kind)
        except GatewayError as err:
            self._finish(call, err)
            raise
        TenantScope.bind(call.grant.tenant_uuid)
        try:
            self.capability.check(call.summary.model, call.grant.model.caps, call.summary)
            call.routes = self.fmt.routes_for(entry, call.summary.model, call.grant.model.routes)
        except GatewayError as err:
            self._finish(call, err)
            raise
        if call.summary.stream:
            return Answer(stream=self._stream(call, body, headers), headers=_stream_headers())
        return await self._once(call, body, headers)

    # ── 非流式 ─────────────────────────────────────────────────────────

    async def _once(
        self, call: Call, body: Mapping[str, Any], headers: Mapping[str, str]
    ) -> Answer:
        assert call.grant and call.summary
        routes = call.routes
        tried: set[UUID] = set()
        try:
            while len(call.attempts) < self.t.max_attempts:
                route = await self.routing.pick(routes, call.session_key, tried=tried)
                if route is None:
                    break
                tried.add(route.uuid)
                attempt = call.attempt(
                    route, self.capability.plan(call.grant.model.caps, route, call.summary)
                )
                upstream: UpstreamCall | None = None
                try:
                    upstream = await self._open(call, attempt, body, headers, stream=False)
                    data, usage = await asyncio.wait_for(
                        upstream.read(), self._first_timeout(route)
                    )
                except (UpstreamError, TimeoutError) as exc:
                    failure = _failure_of(exc)
                    if not await self._failed(attempt, failure):
                        raise ErrorMapper.bad_request(failure) from None
                    continue
                finally:
                    if upstream is not None:
                        await upstream.aclose()
                call.commit(attempt)
                attempt.outcome = "ok"
                call.usage = usage
                answer = Answer(body=json.dumps(data, ensure_ascii=False).encode())
                self._finish(call)
                return answer
            raise ErrorMapper.from_failures(call, routes_exist=bool(routes))
        except GatewayError as err:
            self._finish(call, err)
            raise

    # ── 流式 ───────────────────────────────────────────────────────────

    async def _stream(
        self, call: Call, body: Mapping[str, Any], headers: Mapping[str, str]
    ) -> AsyncIterator[bytes]:
        """逐段的字节。还没写出任何东西时出错就抛 GatewayError（入口回正常的错误状态码），
        写出过（哪怕只是保活注释）就只能在流里发错误事件。"""
        assert call.grant and call.summary
        routes = call.routes
        tried: set[UUID] = set()
        sent = False
        completed = False
        upstream: UpstreamCall | None = None
        error: GatewayError | None = None
        try:
            while True:
                route = (
                    await self.routing.pick(routes, call.session_key, tried=tried)
                    if len(call.attempts) < self.t.max_attempts
                    else None
                )
                if route is None:
                    error = ErrorMapper.from_failures(call, routes_exist=bool(routes))
                    break
                tried.add(route.uuid)
                attempt = call.attempt(
                    route, self.capability.plan(call.grant.model.caps, route, call.summary)
                )
                try:
                    upstream = await self._open(call, attempt, body, headers, stream=True)
                except UpstreamError as exc:
                    if await self._failed(attempt, exc.failure):
                        continue
                    error = ErrorMapper.bad_request(exc.failure)
                    break

                # ── 提交点之前：压住不含内容的段，等第一段内容；定期发保活注释
                pieces = upstream.pieces().__aiter__()
                held: list[bytes] = []
                failure: UpstreamFailure | None = None
                deadline = time.monotonic() + self._first_timeout(route)
                while failure is None:
                    task = asyncio.ensure_future(pieces.__anext__())
                    try:
                        while not task.done():
                            left = deadline - time.monotonic()
                            if left <= 0:
                                failure = UpstreamFailure(
                                    FailureKind.OVERLOADED, "等不到第一段内容（超时）", timeout=True
                                )
                                break
                            done, _ = await asyncio.wait(
                                {task}, timeout=min(self.t.keepalive_s, left)
                            )
                            if not done:
                                sent = True
                                yield KEEPALIVE
                    finally:
                        if not task.done():
                            task.cancel()
                    if failure is not None:
                        break
                    try:
                        piece: Piece = task.result()
                    except StopAsyncIteration:
                        failure = UpstreamFailure(
                            FailureKind.OVERLOADED, "上游没给任何内容就结束了"
                        )
                        break
                    except UpstreamError as exc:
                        failure = exc.failure
                        break
                    except Exception as exc:
                        failure = UpstreamFailure(
                            FailureKind.OVERLOADED, f"读上游的回答时出错：{exc!r}"
                        )
                        break
                    if piece.info.failure is not None:
                        failure = piece.info.failure
                        break
                    self._absorb(call, piece)
                    if piece.data:
                        held.append(piece.data)
                    if piece.info.is_content:
                        break
                if failure is not None:
                    await upstream.aclose()
                    upstream = None
                    if await self._failed(attempt, failure):
                        continue
                    error = ErrorMapper.bad_request(failure)
                    break

                # ── 提交：压着的连同第一段一起写出，之后读一段写一段
                call.commit(attempt)
                attempt.outcome = "ok"
                sent = True
                for data in held:
                    yield data
                while True:
                    try:
                        piece = await asyncio.wait_for(pieces.__anext__(), self.t.idle_s)
                    except StopAsyncIteration:
                        break
                    except TimeoutError:
                        error = ErrorMapper.interrupted(
                            f"两段之间超过 {self.t.idle_s:.0f} 秒没有新内容"
                        )
                        break
                    except Exception as exc:
                        error = ErrorMapper.interrupted(f"读上游的回答时出错：{exc!r}")
                        break
                    self._absorb(call, piece)
                    if piece.info.failure is not None:
                        error = ErrorMapper.interrupted(piece.info.failure.message)
                        if piece.data:  # 上游的错误事件本身已经是入口格式，原样转出
                            yield piece.data
                            error.detail += "（已转给调用方）"
                            call.error, call.status = error, "interrupted"
                            error = None
                        break
                    if piece.data:
                        yield piece.data
                break

            if error is not None:
                if not sent:
                    raise error
                call.error, call.status = error, ("interrupted" if call.committed else "error")
                yield self.fmt.error_event(call.entry, error)
            completed = True
        except asyncio.CancelledError:
            call.status = "interrupted"
            call.error = ErrorMapper.interrupted("调用方断开了")
            raise
        except GatewayError as err:
            call.error, call.status = err, "error"
            raise
        finally:
            if not completed and call.error is None:
                # 调用方断开（写失败、生成器被关掉）：不再计费，记成中途中断
                call.error = ErrorMapper.interrupted("调用方断开了")
                call.status = "interrupted" if call.committed else "error"
            if upstream is not None:
                try:
                    await upstream.aclose()
                except Exception:
                    logger.debug("关上游连接失败", exc_info=True)
            self._finish(call, call.error)

    # ── 数 token（Claude Code 会调） ───────────────────────────────────

    async def count_tokens(
        self, entry: Entry, secret: str | None, body: Mapping[str, Any], headers: Mapping[str, str]
    ) -> dict[str, Any]:
        """有 Anthropic 格式地址的线路转给上游数；没有就按字符估算。不计费、不写调用记录。

        线路和对话一样按入口筛：Anthropic 入口只看 Anthropic 格式的线路。"""
        summary = self.fmt.read(entry, body, headers, ModelKind.CHAT)
        grant = await self.admission.admit(secret, summary.model, ModelKind.CHAT)
        routes = self.fmt.routes_for(entry, summary.model, grant.model.routes)
        route = await self.routing.pick(routes, summary.session_key or str(grant.key.key_uuid))
        if route is not None and route.account.endpoints.get(Format.ANTHROPIC):
            plan = self.capability.plan(grant.model.caps, route, summary)
            try:
                return await self.fmt.count_tokens(body, headers, route, plan)
            except UpstreamError:
                logger.info("上游数 token 失败，改为估算", exc_info=True)
        return {"input_tokens": summary.approx_input_tokens, "estimated": True}

    # ── 小步骤 ─────────────────────────────────────────────────────────

    def _first_timeout(self, route: Route) -> float:
        return route.account.first_content_timeout_s or self.t.first_content_s

    async def _open(
        self,
        call: Call,
        attempt: Attempt,
        body: Mapping[str, Any],
        headers: Mapping[str, str],
        *,
        stream: bool,
    ) -> UpstreamCall:
        assert call.summary
        upstream = await self.fmt.open(
            call.entry,
            call.kind,
            body,
            headers,
            attempt.route,
            attempt.plan,
            model=call.summary.model,
            stream=stream,
            read_timeout_s=self._first_timeout(attempt.route),
        )
        attempt.path = "direct" if upstream.path.direct else f"convert:{upstream.path.upstream}"
        attempt.dropped = upstream.dropped
        return upstream

    async def _failed(self, attempt: Attempt, failure: UpstreamFailure) -> bool:
        """记下这次尝试失败、交给线路判断；返回要不要换下一条线路。"""
        attempt.outcome = _outcome(failure)
        attempt.message = failure.message
        verdict = await self.routing.report(attempt.route, failure)
        logger.info(
            "线路失败：%s %s → %s（%s）",
            attempt.route.account.provider,
            attempt.route.upstream_model,
            attempt.outcome,
            "换下一条" if verdict.switch else "不换",
        )
        return verdict.switch

    @staticmethod
    def _absorb(call: Call, piece: Piece) -> None:
        if piece.info.usage is not None:
            call.usage = call.usage.merge(piece.info.usage)
        if piece.info.is_content and piece.data:
            call.output_chars += max(len(piece.data) - 60, 1)

    def _finish(self, call: Call, error: GatewayError | None = None) -> None:
        """记用量、累加花费、写调用记录。不 await 数据库：记录进队列，花费在后台加。"""
        if error is not None and call.error is None:
            call.error = error
            call.status = "interrupted" if call.committed else "error"
        grant, summary = call.grant, call.summary
        usage = call.usage
        if call.committed and (usage.output == 0 or call.status != "ok"):
            usage = UsageEstimator.estimate(
                usage,
                input_tokens=summary.approx_input_tokens if summary else 0,
                output_chars=call.output_chars,
            )
            call.usage = usage
        price = grant.model.price if grant else None
        cost = self.metering.settle(usage, price) if price else Decimal(0)
        if grant is None:
            return  # 没认出是谁（Key 无效）：没有租户可记
        if cost:
            with TenantScope.isolated():
                asyncio.get_running_loop().create_task(self.admission.settle(grant, cost))
        self.metering.record(
            CallRecord(
                call_uuid=call.call_uuid,
                tenant_uuid=grant.tenant_uuid,
                started_at=call.started_at,
                model=summary.model if summary else "",
                entry_format=call.entry.value,
                status=call.status,
                duration_ms=int((time.monotonic() - call.started) * 1000),
                key_uuid=grant.key.key_uuid,
                key_kind=grant.key.kind,
                session_key=summary.session_key if summary else None,
                error_category=call.error.category.value if call.error else None,
                attempts=[a.as_json() for a in call.attempts],
                first_content_ms=call.first_content_ms,
                usage=usage,
                price=grant.model.price,
                cost=cost,
                notes=_notes(call),
            )
        )


def _notes(call: Call) -> dict[str, Any]:
    notes: dict[str, Any] = {}
    if call.attempts:
        last = call.attempts[-1]
        if last.plan.notes:
            notes["params"] = list(last.plan.notes)
        if last.dropped:
            notes["dropped"] = last.dropped
    if call.error is not None and call.error.detail:
        notes["error"] = call.error.detail[:500]
    return notes


def _failure_of(exc: Exception) -> UpstreamFailure:
    if isinstance(exc, UpstreamError):
        return exc.failure
    return UpstreamFailure(FailureKind.OVERLOADED, "等不到上游的回答（超时）", timeout=True)


def _stream_headers() -> dict[str, str]:
    return {"cache-control": "no-cache", "x-accel-buffering": "no"}
