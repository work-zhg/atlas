"""线路与账号的运行状态（c4-model/modelgateway/code-gateway-routing.html）。

★ 冷却按线路、异常按账号，存在网关状态（Redis）里，所有网关实例与管理平台共用一份键格式；
  网关状态不可用时退回进程内存。
★ 全部可用线路都在冷却时，用半开探测门（try_acquire_probe）限制每个窗口只放一个探测请求去试，
  其余并发请求快速回 429 MODEL_BUSY，避免全量并发击穿冷却把限流上游彻底打死。
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from .kernel import Route

__all__ = ["AccountIssue", "IssueRecord", "RouteHealth"]

logger = logging.getLogger(__name__)

#: 全部线路冷却时，同一条线路多久最多放行一次半开探测
DEFAULT_PROBE_INTERVAL_S = 5.0


class AccountIssue(StrEnum):
    AUTH_FAILED = "auth_failed"
    NO_BALANCE = "no_balance"


@dataclass(frozen=True)
class IssueRecord:
    issue: AccountIssue
    since: float
    message: str = ""


def _issue(raw: bytes | str) -> IssueRecord:
    data = json.loads(raw)
    return IssueRecord(AccountIssue(data["issue"]), float(data["since"]), data.get("message", ""))


class RouteHealth:
    """线路冷却、账号异常与半开探测门。管理平台展示状态与巡检恢复也用它。"""

    def __init__(
        self,
        redis: Any | None,
        *,
        prefix: str = "modelgw",
        probe_interval_s: float = DEFAULT_PROBE_INTERVAL_S,
    ) -> None:
        self._redis = redis
        self._prefix = prefix
        self._probe_interval_s = probe_interval_s
        self._cool: dict[UUID, float] = {}
        self._issues: dict[UUID, IssueRecord] = {}
        self._probe_until: dict[UUID, float] = {}

    def _cool_key(self, route: UUID) -> str:
        return f"{self._prefix}:cool:{route}"

    def _issue_key(self, account: UUID) -> str:
        return f"{self._prefix}:issue:{account}"

    def _probe_key(self, route: UUID) -> str:
        return f"{self._prefix}:probe:{route}"

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
        # 新进冷却时重置本线路的半开探测锁，允许首个兜底探测按间隔竞争一次
        self._probe_until.pop(route, None)
        if self._redis is not None:
            try:
                await self._redis.set(self._cool_key(route), str(until), ex=max(int(seconds), 1))
            except Exception:
                logger.warning("网关状态不可用，线路冷却只记在本实例", exc_info=True)

    async def try_acquire_probe(self, route: UUID, *, interval_s: float | None = None) -> bool:
        """全部线路都在冷却时抢一次半开探测权：窗口内只放行 1 个请求试探上游，其余快速回 429。"""
        window = interval_s if interval_s is not None else self._probe_interval_s
        if window <= 0:
            return True
        if self._redis is not None:
            try:
                ok = await self._redis.set(
                    self._probe_key(route), "1", nx=True, ex=max(math.ceil(window), 1)
                )
                return bool(ok)
            except Exception:
                logger.warning("网关状态不可用，半开探测退回进程内存", exc_info=True)
        now = time.time()
        if self._probe_until.get(route, 0.0) > now:
            return False
        self._probe_until[route] = now + window
        return True

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
