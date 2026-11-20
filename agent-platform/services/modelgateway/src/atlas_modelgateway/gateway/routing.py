"""线路（c4-model/modelgateway/code-gateway-routing.html）：走哪个账号的哪个上游模型，失败了换不换。

★ 分配的单位是会话：同一个会话每一轮都落在同一条线路上，缓存才能命中（缓存只在同一个账号里有效）。
★ 按失败的种类判断，不看状态码；种类由供应商方言归好。
★ 冷却按线路、异常按账号，存在网关状态里，所有网关实例共用；网关状态不可用时退回进程内存。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from .kernel import FailureKind, Route, UpstreamFailure

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


class AccountIssue(StrEnum):
    AUTH_FAILED = "auth_failed"
    NO_BALANCE = "no_balance"


@dataclass(frozen=True)
class IssueRecord:
    issue: AccountIssue
    since: float
    message: str = ""


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
    """加权的一致性哈希（rendezvous）：每条线路按「会话标识 + 线路 id」算分，分最高的是落点。

    不用在任何地方记「会话 → 线路」；线路增减时只有落在变动线路上的会话会挪。
    """

    @staticmethod
    def score(session_key: str, route: Route) -> float:
        digest = hashlib.sha256(f"{session_key}|{route.uuid}".encode()).digest()
        unit = (int.from_bytes(digest[:8], "big") + 1) / (2**64 + 2)  # (0, 1)
        return -max(route.weight, 1) / math.log(unit)

    @classmethod
    def rank(cls, session_key: str, routes: Iterable[Route]) -> list[Route]:
        return sorted(routes, key=lambda r: cls.score(session_key, r), reverse=True)


class RouteHealth:
    """线路冷却、账号异常。键的格式只有这一份：管理平台解除异常也用它。"""

    def __init__(self, redis: Any | None, *, prefix: str = "modelgw") -> None:
        self._redis = redis
        self._prefix = prefix
        self._cool: dict[UUID, float] = {}
        self._issues: dict[UUID, IssueRecord] = {}

    def _cool_key(self, route: UUID) -> str:
        return f"{self._prefix}:cool:{route}"

    def _issue_key(self, account: UUID) -> str:
        return f"{self._prefix}:issue:{account}"

    async def snapshot(
        self, routes: Sequence[Route]
    ) -> tuple[dict[UUID, float], dict[UUID, IssueRecord]]:
        """这些线路的冷却到期时间、它们账号的异常：一次往返。"""
        return await self.lookup(
            [r.uuid for r in routes], list(dict.fromkeys(r.account.uuid for r in routes))
        )

    async def lookup(
        self, routes: Sequence[UUID], accounts: Sequence[UUID]
    ) -> tuple[dict[UUID, float], dict[UUID, IssueRecord]]:
        if self._redis is not None and (routes or accounts):
            try:
                keys = [self._cool_key(r) for r in routes] + [self._issue_key(a) for a in accounts]
                values = await self._redis.mget(keys)
                cool = {
                    r: float(v) for r, v in zip(routes, values[: len(routes)], strict=True) if v
                }
                issues = {
                    a: _issue(v) for a, v in zip(accounts, values[len(routes) :], strict=True) if v
                }
                return cool, issues
            except Exception:
                logger.warning("网关状态不可用，线路状态退回进程内存", exc_info=True)
        now = time.time()
        cool = {r: t for r in routes if (t := self._cool.get(r, 0)) > now}
        issues = {a: i for a in accounts if (i := self._issues.get(a)) is not None}
        return cool, issues

    async def cool(self, route: UUID, seconds: float) -> None:
        until = time.time() + seconds
        self._cool[route] = until
        if self._redis is not None:
            try:
                await self._redis.set(self._cool_key(route), str(until), ex=max(int(seconds), 1))
            except Exception:
                logger.warning("网关状态不可用，线路冷却只记在本实例", exc_info=True)

    async def mark(self, account: UUID, issue: AccountIssue, message: str = "") -> None:
        record = IssueRecord(issue, time.time(), message[:300])
        self._issues[account] = record
        if self._redis is not None:
            try:
                payload = {"issue": issue.value, "since": record.since, "message": record.message}
                await self._redis.set(
                    self._issue_key(account), json.dumps(payload, ensure_ascii=False)
                )
            except Exception:
                logger.warning("网关状态不可用，账号异常只记在本实例", exc_info=True)

    async def clear(self, account: UUID) -> None:
        self._issues.pop(account, None)
        if self._redis is not None:
            await self._redis.delete(self._issue_key(account))

    async def abnormal_accounts(self) -> dict[UUID, IssueRecord]:
        """管理平台检查异常账号用：全部异常的账号。"""
        if self._redis is None:
            return dict(self._issues)
        found: dict[UUID, IssueRecord] = {}
        async for key in self._redis.scan_iter(match=f"{self._prefix}:issue:*", count=200):
            raw = await self._redis.get(key)
            name = key.decode() if isinstance(key, bytes) else str(key)
            if raw:
                found[UUID(name.rsplit(":", 1)[1])] = _issue(raw)
        return found


def _issue(raw: bytes | str) -> IssueRecord:
    data = json.loads(raw)
    return IssueRecord(AccountIssue(data["issue"]), float(data["since"]), data.get("message", ""))


class RouteSelector:
    def __init__(self, health: RouteHealth) -> None:
        self.health = health

    async def pick(
        self, routes: Sequence[Route], session_key: str, *, tried: Iterable[UUID] = ()
    ) -> Route | None:
        """按优先级分组，组里按会话落点排序；跳过停用、账号异常、冷却中和试过的。

        整组都不行就下一组；全部都在冷却时选冷却最早结束的那条；一条都没有返回 None。
        """
        tried = set(tried)
        cooling, issues = await self.health.snapshot(routes)
        usable = [
            r
            for r in routes
            if r.account.enabled and r.uuid not in tried and r.account.uuid not in issues
        ]
        if not usable:
            return None
        now = time.time()
        for priority in sorted({r.priority for r in usable}):
            group = [r for r in usable if r.priority == priority]
            for route in StickyPlacement.rank(session_key, group):
                if cooling.get(route.uuid, 0) <= now:
                    return route
        return min(usable, key=lambda r: cooling.get(r.uuid, 0))

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
