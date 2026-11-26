"""线路（c4-model/modelgateway/code-gateway-routing.html）：走哪个账号的哪个上游模型，失败了换不换。

★ 有会话标识的对话按会话落点（rendezvous 加权一致性哈希），保证提示词缓存命中；
  无会话的客户端调用及向量、重排没有提示词缓存，按权重随机分摊流量（Efraimidis-Spirakis 加权抽样），
  避免整把共享 Key 的全部流量死钉在单条线路上。
★ 按失败的种类判断，不看状态码；种类由供应商方言归好。
★ 全部可用线路都在冷却时走半开单飞探测：窗口内只放 1 个探测请求试冷却最早结束的线路，
  其余并发请求返回 None，由调用编排快速回 429 MODEL_BUSY。
"""

from __future__ import annotations

import hashlib
import logging
import math
import random
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from uuid import UUID

from ..shared.health import AccountIssue, IssueRecord, RouteHealth
from ..shared.kernel import FailureKind, Route, UpstreamFailure

__all__ = [
    "AccountIssue",
    "FailurePolicy",
    "IssueRecord",
    "RouteHealth",
    "RouteSelector",
    "StickyPlacement",
    "Verdict",
]

logger = logging.getLogger(__name__)

#: 没说等多久时，线路冷却 60 秒
DEFAULT_COOL_S = 60.0
#: 供应商说的等待时间最长按 1 小时算（有的按 5 小时窗口给额度，等太久不如让管理员处理）
MAX_COOL_S = 3600.0


@dataclass(frozen=True)
class Verdict:
    switch: bool
    cool_route_s: float | None = None
    account_issue: AccountIssue | None = None


class FailurePolicy:
    @staticmethod
    def verdict(failure: UpstreamFailure) -> Verdict:
        match failure.kind:
            case FailureKind.BAD_REQUEST:
                return Verdict(switch=False)
            case FailureKind.RATE_LIMITED:
                wait = failure.retry_after_s if failure.retry_after_s else DEFAULT_COOL_S
                return Verdict(switch=True, cool_route_s=min(max(wait, 1.0), MAX_COOL_S))
            case FailureKind.AUTH_FAILED:
                return Verdict(switch=True, account_issue=AccountIssue.AUTH_FAILED)
            case FailureKind.NO_BALANCE:
                return Verdict(switch=True, account_issue=AccountIssue.NO_BALANCE)
            case _:
                return Verdict(switch=True, cool_route_s=DEFAULT_COOL_S)


class StickyPlacement:
    """加权的一致性哈希（rendezvous）与无会话时的同构加权随机排序。

    不用在任何地方记「会话 → 线路」；线路增减时只有落在变动线路上的会话会挪。
    没有会话标识时（客户端无状态调用、向量、重排），用相同指数分布公式按权重随机排序。
    """

    @staticmethod
    def score(session_key: str, route: Route) -> float:
        digest = hashlib.sha256(f"{session_key}|{route.uuid}".encode()).digest()
        unit = (int.from_bytes(digest[:8], "big") + 1) / (2**64 + 2)  # (0, 1)
        return -max(route.weight, 1) / math.log(unit)

    @classmethod
    def rank(cls, session_key: str, routes: Iterable[Route]) -> list[Route]:
        return sorted(routes, key=lambda r: cls.score(session_key, r), reverse=True)

    @staticmethod
    def weighted_order(routes: Iterable[Route]) -> list[Route]:
        return sorted(
            routes,
            key=lambda r: -max(r.weight, 1) / math.log(random.uniform(1e-12, 1.0 - 1e-12)),
            reverse=True,
        )


class RouteSelector:
    def __init__(self, health: RouteHealth) -> None:
        self.health = health

    async def pick(
        self,
        routes: Sequence[Route],
        session_key: str | None = None,
        *,
        tried: Iterable[UUID] = (),
    ) -> Route | None:
        """按优先级分组，有会话时按落点粘连，无会话时按权重随机分流；跳过停用、账号异常、冷却中和试过的。

        整组都不行就下一组；全部可用线路都在冷却时，仅在抢到半开探测锁时放行冷却最早结束的那条，
        否则返回 None（保护被限流的上游不被并发请求冲垮）。
        """
        tried_set = set(tried)
        cooling, issues = await self.health.snapshot(routes)
        usable = [
            r
            for r in routes
            if r.account.enabled and r.uuid not in tried_set and r.account.uuid not in issues
        ]
        if not usable:
            return None
        now = time.time()
        for priority in sorted({r.priority for r in usable}):
            group = [r for r in usable if r.priority == priority]
            ordered = (
                StickyPlacement.rank(session_key, group)
                if session_key
                else StickyPlacement.weighted_order(group)
            )
            for route in ordered:
                if cooling.get(route.uuid, 0) <= now:
                    return route
        candidate = min(usable, key=lambda r: cooling.get(r.uuid, 0))
        if await self.health.try_acquire_probe(candidate.uuid):
            return candidate
        return None

    async def cooling_retry_after(self, routes: Sequence[Route]) -> float | None:
        """可用线路全部冷却时，给出最早恢复的剩余秒数；若无可用线路（都停用/异常）返回 None。"""
        cooling, issues = await self.health.snapshot(routes)
        usable = [r for r in routes if r.account.enabled and r.account.uuid not in issues]
        if not usable:
            return None
        now = time.time()
        waits = [cooling.get(r.uuid, 0.0) - now for r in usable]
        earliest = min(waits)
        return max(1.0, earliest) if earliest > 0 else 1.0

    async def report(self, route: Route, failure: UpstreamFailure) -> Verdict:
        verdict = FailurePolicy.verdict(failure)
        if verdict.cool_route_s:
            await self.health.cool(route.uuid, verdict.cool_route_s)
        if verdict.account_issue:
            await self.health.mark(route.account.uuid, verdict.account_issue, failure.message)
        return verdict

    async def unusable_reasons(self, routes: Sequence[Route]) -> dict[UUID, IssueRecord]:
        """没有可用线路时给管理员看的原因：哪些账号异常。"""
        _, issues = await self.health.snapshot(routes)
        return issues
