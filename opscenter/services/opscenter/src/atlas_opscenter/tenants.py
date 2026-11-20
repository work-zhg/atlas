"""租户（c4-model/opscenter/code-tenants.html）：开通、停用、恢复、关闭，每种拆成按系统的步骤。

    开通   用户中心建租户（含根部门、首个管理员、开通的应用）→ 各系统设份额 → 技能应用装默认技能
    停用   用户中心挡住入口 → 各系统挂起（「各系统」= 登记里声明了租户生命周期的，按登记顺序）
    恢复   各系统恢复 → 用户中心放开入口
    关闭   各系统删数据（分批，删完为止）→ 用户中心删租户

★ 一次操作记成 TenantOperation（ops_tenant_operation + ops_operation_step），后台任务推进，每做完
  一步保存一次；失败不放弃，退避后一直重试（对方明确拒绝的退避更长、界面标红），可以立即重做。
★ 开通的第一步在接口里同步跑：临时密码要当场交给运营人员，不落库、不写审计；没拿到时「补发」。
  第一步被用户中心明确拒绝（企业代码重复等）时，这次开通整个作废，什么都没留下。
★ 每一步用 step_uuid 作幂等键；开通时租户 uuid 由运营中心先生成，用户中心按它幂等建。
★ 租户本身（名单、名字、状态、到期）不在运营库里存一份，一律问用户中心。
"""

from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from atlas_jobs import Job, JobContext, NoParams
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .audit import Who, complete, record
from .db.models import OperationStep, TenantOperation
from .db.types import utcnow
from .errors import Conflict, Invalid, NotFound, OpsError, Upstream
from .gateway import CallFailed, Capability, OpsClient, SystemRegistry, stamp_of

__all__ = [
    "ExpiryWatcher",
    "OpenRequest",
    "OperationSweeper",
    "StepRunner",
    "TenantOps",
    "operation_view",
]

logger = logging.getLogger(__name__)

#: 退避：可以重做的从 10 秒起翻倍、最长 10 分钟；被拒绝的从 5 分钟起翻倍、最长 1 小时
_RETRY = (timedelta(seconds=10), timedelta(minutes=10))
_REJECT = (timedelta(minutes=5), timedelta(hours=1))
#: 一次推进最多连做多少步（删除一批算一步）：别让一个大租户占住实例
_MAX_STEPS_PER_ADVANCE = 50


def backoff(attempts: int, *, rejected: bool) -> timedelta:
    first, cap = _REJECT if rejected else _RETRY
    return min(first * (2 ** max(attempts - 1, 0)), cap)


@dataclass
class OpenRequest:
    code: str
    name: str
    admin: dict[str, str]
    max_users: int | None = None
    expires_at: datetime | None = None
    #: 开通的应用编码
    apps: list[str] = field(default_factory=list)
    #: 各系统的份额：{系统编码: 份额}；没给的系统用默认份额
    quotas: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: 装技能市场里「创建租户时默认安装」的技能
    install_default_skills: bool = True


class Planner:
    """把一种操作排成步骤（顺序规则都在这里）。"""

    def __init__(self, registry: SystemRegistry) -> None:
        self.registry = registry

    def plan(self, kind: str, params: dict[str, Any]) -> list[tuple[str, str]]:
        uc = self.registry.user_center.code
        life = [e.code for e in self.registry.with_capability(Capability.LIFECYCLE)]
        if kind == "OPEN":
            steps = [(uc, "CREATE_TENANT")]
            for e in self.registry.with_capability(Capability.CAPACITY):
                if e.code in (params.get("quotas") or {}):
                    steps.append((e.code, "SET_QUOTA"))
            if params.get("install_default_skills"):
                steps += [
                    (e.code, "INSTALL_SKILLS")
                    for e in self.registry.with_capability(Capability.SKILL_MARKET)
                ]
            return steps
        if kind == "SUSPEND":
            return [(uc, "BLOCK"), *[(s, "SUSPEND") for s in life]]
        if kind == "RESUME":
            return [*[(s, "RESUME") for s in life], (uc, "UNBLOCK")]
        if kind == "CLOSE":
            return [*[(s, "PURGE") for s in life], (uc, "DELETE_TENANT")]
        raise ValueError(kind)


def operation_view(op: TenantOperation, steps: list[OperationStep]) -> dict[str, Any]:
    return {
        "id": str(op.operation_uuid),
        "tenant_id": str(op.tenant_uuid),
        "tenant_code": op.tenant_code,
        "kind": op.kind,
        "status": op.status,
        "reason": op.reason,
        "requested_by": op.requested_by_name,
        "created_at": op.created_at.isoformat(),
        "finished_at": op.finished_at.isoformat() if op.finished_at else None,
        "stuck": any(s.status == "FAILED" for s in steps),
        "steps": [
            {
                "id": str(s.step_uuid),
                "seq": s.seq,
                "system": s.system,
                "action": s.action,
                "status": s.status,
                "attempts": s.attempts,
                "next_attempt_at": s.next_attempt_at.isoformat(),
                "last_error": s.last_error,
                "rejected": s.rejected,
                "result": s.result,
                "finished_at": s.finished_at.isoformat() if s.finished_at else None,
            }
            for s in steps
        ],
    }


async def _steps(session: AsyncSession, op: TenantOperation) -> list[OperationStep]:
    return list(
        await session.scalars(
            select(OperationStep)
            .where(OperationStep.operation_uuid == op.operation_uuid)
            .order_by(OperationStep.seq)
        )
    )


# ───────────────────────────── 推进


class StepRunner:
    """推进一个操作：取下一步，调对方的运营接口（带幂等键和运营人员），按结果记成功或失败。"""

    def __init__(self, client: OpsClient) -> None:
        self.client = client

    async def advance(self, session: AsyncSession, op: TenantOperation) -> bool:
        """→ 操作做完了没有。每做完一步提交一次：崩在半路从下一步接着做。"""
        steps = await _steps(session, op)
        for _ in range(_MAX_STEPS_PER_ADVANCE):
            step = next((s for s in steps if s.status != "DONE"), None)
            if step is None:
                await self._finish(session, op)
                return True
            now = utcnow()
            if step.next_attempt_at > now:
                return False
            try:
                done, result = await self._execute(op, step)
            except CallFailed as exc:
                step.status = "FAILED"
                step.attempts += 1
                step.rejected = not exc.retryable
                step.last_error = exc.message[:2000]
                step.next_attempt_at = now + backoff(step.attempts, rejected=step.rejected)
                await session.commit()
                logger.warning(
                    "租户 %s 的 %s：%s/%s 失败（第 %d 次）：%s",
                    op.tenant_code,
                    op.kind,
                    step.system,
                    step.action,
                    step.attempts,
                    exc.message,
                )
                return False
            step.result = _merge(step.result, result)
            step.last_error, step.rejected = None, False
            if done:
                step.status = "DONE"
                step.finished_at = now
            else:
                # 删除没删完：这一步保持未完成，马上再做一批（同一个幂等键）
                step.status = "PENDING"
                step.attempts += 1
                step.next_attempt_at = now
            await session.commit()
        return False

    async def _finish(self, session: AsyncSession, op: TenantOperation) -> None:
        op.status = "DONE"
        op.finished_at = utcnow()
        op.lease_owner = op.lease_until = None
        if op.audit_uuid is not None:
            await complete(session, op.audit_uuid, "OK")
        await session.commit()
        logger.info("租户 %s 的 %s 做完了", op.tenant_code, op.kind)

    async def _execute(
        self, op: TenantOperation, step: OperationStep
    ) -> tuple[bool, dict[str, Any] | None]:
        stamp = stamp_of(
            op.requested_by,
            op.requested_by_name,
            operation=op.operation_uuid,
            idempotency_key=step.step_uuid,
        )
        t = op.tenant_uuid
        call = self.client.call
        params = op.params or {}
        match step.action:
            case "CREATE_TENANT":
                out = await call(step.system, "POST", "/tenants", params["create"], stamp=stamp)
                # ★ 临时密码不留：同步那一次已经交给运营人员；重做出来的不管（要的话补发）
                return True, {"admin_account": (out or {}).get("admin", {}).get("account")}
            case "SET_QUOTA":
                quota = (params.get("quotas") or {}).get(step.system) or {}
                await call(step.system, "PUT", f"/quotas/{t}", quota, stamp=stamp)
                return True, None
            case "INSTALL_SKILLS":
                out = await call(
                    step.system, "POST", f"/tenants/{t}/market-skills", {"skills": []}, stamp=stamp
                )
                return True, {"installed": len(out or [])}
            case "BLOCK" | "UNBLOCK":
                await call(step.system, "POST", f"/tenants/{t}/{step.action.lower()}", stamp=stamp)
                return True, None
            case "SUSPEND" | "RESUME":
                await call(step.system, "POST", f"/tenants/{t}/{step.action.lower()}", stamp=stamp)
                return True, None
            case "PURGE":
                out = await call(step.system, "POST", f"/tenants/{t}/purge", stamp=stamp) or {}
                return not out.get("remaining", False), {"deleted": out.get("deleted") or {}}
            case "DELETE_TENANT":
                out = await call(step.system, "DELETE", f"/tenants/{t}", stamp=stamp) or {}
                return True, {"deleted": out.get("deleted") or {}}
        raise ValueError(step.action)


def _merge(old: dict[str, Any] | None, new: dict[str, Any] | None) -> dict[str, Any] | None:
    """删除是分批的：各批删了多少条累加起来。"""
    if not new:
        return old
    out = dict(old or {})
    for k, v in new.items():
        if k == "deleted" and isinstance(v, dict):
            acc = dict(out.get("deleted") or {})
            for name, n in v.items():
                acc[name] = acc.get(name, 0) + int(n or 0)
            out["deleted"] = acc
        else:
            out[k] = v
    return out


# ───────────────────────────── 入口


class TenantOps:
    def __init__(self, session: AsyncSession, client: OpsClient) -> None:
        self.session = session
        self.client = client
        self.registry = client.registry
        self.planner = Planner(client.registry)

    @property
    def _uc(self) -> str:
        return self.registry.user_center.code

    async def _uc_call(self, method: str, path: str, payload: Any = None, *, who: Who) -> Any:
        """直接问用户中心（不属于某个操作的读写）。失败翻成接口错误。"""
        try:
            return await self.client.call(
                self._uc, method, path, payload, stamp=stamp_of(who.operator_uuid, who.name)
            )
        except CallFailed as exc:
            if exc.status == 404:
                raise NotFound("TENANT_NOT_FOUND", "租户不存在") from exc
            if exc.retryable:
                raise Upstream("UPSTREAM_UNAVAILABLE", exc.message) from exc
            raise OpsError("UPSTREAM_REJECTED", exc.message) from exc

    # ── 查

    async def list(self, who: Who) -> list[dict[str, Any]]:
        tenants = await self._uc_call("GET", "/tenants", who=who) or []
        running = {o.tenant_uuid: o for o in await self._running()}
        out = []
        for t in tenants:
            op = running.get(UUID(t["id"]))
            out.append({**t, "operation": await self._brief(op) if op else None})
        return out

    async def get(self, tenant_uuid: UUID, who: Who) -> dict[str, Any]:
        return await self._uc_call("GET", f"/tenants/{tenant_uuid}", who=who)  # type: ignore[no-any-return]

    async def operations(self, tenant_uuid: UUID) -> list[dict[str, Any]]:
        ops = list(
            await self.session.scalars(
                select(TenantOperation)
                .where(TenantOperation.tenant_uuid == tenant_uuid)
                .order_by(TenantOperation.created_at.desc())
            )
        )
        return [operation_view(op, await _steps(self.session, op)) for op in ops]

    async def running(self) -> list[dict[str, Any]]:
        return [operation_view(op, await _steps(self.session, op)) for op in await self._running()]

    async def _running(self) -> list[TenantOperation]:
        return list(
            await self.session.scalars(
                select(TenantOperation)
                .where(TenantOperation.status == "RUNNING")
                .order_by(TenantOperation.created_at)
            )
        )

    async def _brief(self, op: TenantOperation) -> dict[str, Any]:
        steps = await _steps(self.session, op)
        return {
            "id": str(op.operation_uuid),
            "kind": op.kind,
            "stuck": any(s.status == "FAILED" for s in steps),
        }

    # ── 开通

    async def open(self, req: OpenRequest, by: Who) -> dict[str, Any]:
        tenant_uuid = uuid4()
        create = {
            "uuid": str(tenant_uuid),
            "code": (req.code or "").strip().lower(),
            "name": req.name,
            "admin": req.admin,
            "max_users": req.max_users,
            "expires_at": req.expires_at.isoformat() if req.expires_at else None,
            "apps": req.apps,
        }
        for system in req.quotas:
            entry = self.registry.get(system)
            if entry is None or not entry.has(Capability.CAPACITY):
                raise Invalid("VALIDATION_FAILED", f"系统 {system} 没有份额", field="quotas")
        params = {
            "create": create,
            "quotas": req.quotas,
            "install_default_skills": req.install_default_skills,
        }
        op, steps = await self._start(tenant_uuid, create["code"], "OPEN", by, params, None)
        first = steps[0]
        stamp = stamp_of(
            by.operator_uuid, by.name, operation=op.operation_uuid, idempotency_key=first.step_uuid
        )
        try:
            out = await self.client.call(self._uc, "POST", "/tenants", create, stamp=stamp)
        except CallFailed as exc:
            if not exc.retryable:
                # 用户中心明确拒绝（企业代码重复、格式不对……）：这次开通作废，什么都没留下
                raise OpsError("TENANT_REJECTED", exc.message) from exc
            first.status = "FAILED"
            first.attempts = 1
            first.last_error = exc.message[:2000]
            first.next_attempt_at = utcnow() + backoff(1, rejected=False)
            return {
                "operation": operation_view(op, steps),
                "tenant": None,
                "admin": None,
                "temp_password": None,
                "message": (
                    "用户中心暂时连不上，后台会接着建；建好后用「补发临时密码」拿首个管理员的密码"
                ),
            }
        first.status = "DONE"
        first.attempts = 1
        first.finished_at = utcnow()
        first.result = {"admin_account": out["admin"]["account"]}
        if len(steps) == 1:
            # 只有建租户这一步（没给份额、没有技能市场）：这次开通就做完了
            op.status, op.finished_at = "DONE", utcnow()
            entry_uuid = op.audit_uuid
            if entry_uuid is not None:
                await self.session.flush()
                await complete(self.session, entry_uuid, "OK")
        return {
            "operation": operation_view(op, steps),
            "tenant": out["tenant"],
            "admin": out["admin"],
            # ★ 只在这次响应里出现：不落库、不写审计
            "temp_password": out["temp_password"],
            "message": None,
        }

    # ── 停用、恢复、关闭

    async def suspend(self, tenant_uuid: UUID, reason: str | None, by: Who) -> dict[str, Any]:
        t = await self.get(tenant_uuid, by)
        op, steps = await self._start(tenant_uuid, t["code"], "SUSPEND", by, {}, reason or "停用")
        return operation_view(op, steps)

    async def resume(self, tenant_uuid: UUID, by: Who) -> dict[str, Any]:
        t = await self.get(tenant_uuid, by)
        if t["status"] == "active" and not await self._was_suspended(tenant_uuid):
            raise Conflict("TENANT_ACTIVE", "这个租户没有停用")
        op, steps = await self._start(tenant_uuid, t["code"], "RESUME", by, {}, None)
        return operation_view(op, steps)

    async def close(self, tenant_uuid: UUID, confirm_code: str, by: Who) -> dict[str, Any]:
        t = await self.get(tenant_uuid, by)
        if t["status"] != "suspended":
            raise Conflict("TENANT_NOT_SUSPENDED", "只能关闭已停用的租户：先停用")
        if (confirm_code or "").strip().lower() != t["code"]:
            raise Invalid("CONFIRM_MISMATCH", "企业代码输得不对", field="confirm_code")
        op, steps = await self._start(tenant_uuid, t["code"], "CLOSE", by, {}, None)
        return operation_view(op, steps)

    async def _was_suspended(self, tenant_uuid: UUID) -> bool:
        """用户中心那边已经放开了，但上一次停用可能做了一半：看最后一次操作是不是停用。"""
        last = await self.session.scalar(
            select(TenantOperation)
            .where(TenantOperation.tenant_uuid == tenant_uuid)
            .order_by(TenantOperation.created_at.desc())
            .limit(1)
        )
        return last is not None and last.kind == "SUSPEND"

    async def _start(
        self,
        tenant_uuid: UUID,
        code: str,
        kind: str,
        by: Who,
        params: dict[str, Any],
        reason: str | None,
    ) -> tuple[TenantOperation, list[OperationStep]]:
        if await self.session.scalar(
            select(TenantOperation.id).where(
                TenantOperation.tenant_uuid == tenant_uuid, TenantOperation.status == "RUNNING"
            )
        ):
            raise Conflict("OPERATION_RUNNING", "这个租户还有进行中的操作，等它做完再来")
        entry = record(
            self.session,
            by,
            f"tenant.{kind.lower()}",
            target=code,
            tenant_uuid=tenant_uuid,
            detail=_audit_detail(params, reason),
        )
        op = TenantOperation(
            operation_uuid=uuid4(),
            tenant_uuid=tenant_uuid,
            tenant_code=code,
            kind=kind,
            status="RUNNING",
            reason=reason,
            params=params or None,
            requested_by=by.operator_uuid,
            requested_by_name=by.name,
            audit_uuid=entry.audit_uuid,
        )
        self.session.add(op)
        steps = [
            OperationStep(
                step_uuid=uuid4(),
                operation_uuid=op.operation_uuid,
                seq=i,
                system=system,
                action=action,
                status="PENDING",
                attempts=0,
                next_attempt_at=utcnow(),
                rejected=False,
            )
            for i, (system, action) in enumerate(self.planner.plan(kind, params), start=1)
        ]
        self.session.add_all(steps)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            if "uq_ops_tenant_operation_running" not in str(exc.orig):
                raise
            raise Conflict("OPERATION_RUNNING", "这个租户还有进行中的操作，等它做完再来") from exc
        return op, steps

    async def retry_now(self, operation_uuid: UUID, by: Who) -> dict[str, Any]:
        op = await self.session.scalar(
            select(TenantOperation).where(TenantOperation.operation_uuid == operation_uuid)
        )
        if op is None:
            raise NotFound("OPERATION_NOT_FOUND", "操作不存在")
        steps = await _steps(self.session, op)
        for s in steps:
            if s.status == "FAILED":
                s.next_attempt_at = utcnow()
        record(self.session, by, "tenant.retry", target=op.tenant_code, tenant_uuid=op.tenant_uuid)
        return operation_view(op, steps)

    # ── 不属于某个操作的

    async def update(self, tenant_uuid: UUID, data: dict[str, Any], by: Who) -> dict[str, Any]:
        out = await self._uc_call("PATCH", f"/tenants/{tenant_uuid}", data, who=by)
        record(
            self.session,
            by,
            "tenant.update",
            target=out["code"],
            tenant_uuid=tenant_uuid,
            detail=data,
            result="OK",
        )
        return out  # type: ignore[no-any-return]

    async def reissue_admin_password(self, tenant_uuid: UUID, by: Who) -> dict[str, Any]:
        out = await self._uc_call("POST", f"/tenants/{tenant_uuid}/admin-password", who=by)
        # ★ 临时密码只在响应里：审计只记做了这件事
        record(
            self.session,
            by,
            "tenant.reissue_admin_password",
            target=out["admin"]["account"],
            tenant_uuid=tenant_uuid,
            result="OK",
        )
        return out  # type: ignore[no-any-return]


def _audit_detail(params: dict[str, Any], reason: str | None) -> dict[str, Any] | None:
    detail: dict[str, Any] = {}
    if reason:
        detail["reason"] = reason
    create = params.get("create")
    if create:
        detail.update(
            {
                "name": create.get("name"),
                "admin": (create.get("admin") or {}).get("account"),
                "max_users": create.get("max_users"),
                "expires_at": create.get("expires_at"),
                "apps": create.get("apps"),
            }
        )
    if params.get("quotas"):
        detail["quotas"] = params["quotas"]
    return detail or None


# ───────────────────────────── 后台任务


def _instance() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class OperationSweeper(Job):
    """每个实例都跑：分页取有到期步骤的操作，逐个抢租约推进。重启、某一步失败后都靠它接着做。"""

    type = "opscenter.operation_sweep"
    description = "推进租户操作（开通、停用、恢复、关闭）的下一步"

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], runner: StepRunner, lease_s: float
    ) -> None:
        self._sessions = sessions
        self._runner = runner
        self._lease = timedelta(seconds=lease_s)
        self._owner = _instance()

    async def execute(self, ctx: JobContext, params: NoParams) -> None:
        await self.tick()

    async def tick(self) -> int:
        now = utcnow()
        free = or_(TenantOperation.lease_until.is_(None), TenantOperation.lease_until < now)
        undone = select(OperationStep.id).where(
            OperationStep.operation_uuid == TenantOperation.operation_uuid,
            OperationStep.status != "DONE",
        )
        async with self._sessions() as s:
            due = list(
                await s.scalars(
                    select(TenantOperation.operation_uuid)
                    .join(
                        OperationStep,
                        OperationStep.operation_uuid == TenantOperation.operation_uuid,
                    )
                    .where(
                        TenantOperation.status == "RUNNING",
                        OperationStep.status != "DONE",
                        OperationStep.next_attempt_at <= now,
                        free,
                    )
                    .distinct()
                    .limit(20)
                )
            )
            # 步骤都做完了、却还没收尾的（收尾那一刻进程退出了）
            due += list(
                await s.scalars(
                    select(TenantOperation.operation_uuid)
                    .where(TenantOperation.status == "RUNNING", free, ~undone.exists())
                    .limit(20)
                )
            )
        done = 0
        for op_uuid in due:
            if await self._advance(op_uuid):
                done += 1
        return done

    async def _advance(self, op_uuid: UUID) -> bool:
        now = utcnow()
        async with self._sessions() as s:
            claimed = await s.execute(
                update(TenantOperation)
                .where(
                    TenantOperation.operation_uuid == op_uuid,
                    TenantOperation.status == "RUNNING",
                    or_(TenantOperation.lease_until.is_(None), TenantOperation.lease_until < now),
                )
                .values(lease_owner=self._owner, lease_until=now + self._lease)
            )
            await s.commit()
            if getattr(claimed, "rowcount", 0) != 1:
                return False  # 别的实例抢到了
            op = await s.scalar(
                select(TenantOperation).where(TenantOperation.operation_uuid == op_uuid)
            )
            assert op is not None
            try:
                return await self._runner.advance(s, op)
            except Exception:
                logger.exception("推进租户操作 %s 出错", op_uuid)
                return False
            finally:
                if op.status == "RUNNING":
                    op.lease_owner = op.lease_until = None
                    await s.commit()


class ExpiryWatcher(Job):
    """到期的租户自动走停用流程（原因：到期）。已有进行中的操作就跳过，下一轮再看。"""

    type = "opscenter.tenant_expiry"
    description = "给已过到期时间的租户发起停用"

    def __init__(self, sessions: async_sessionmaker[AsyncSession], client: OpsClient) -> None:
        self._sessions = sessions
        self._client = client

    async def execute(self, ctx: JobContext, params: NoParams) -> None:
        await self.tick()

    async def tick(self) -> int:
        who = Who.system()
        async with self._sessions() as s:
            ops = TenantOps(s, self._client)
            try:
                expired = await ops._uc_call("GET", "/tenants/expired", who=who) or []
            except OpsError as exc:
                logger.warning("问用户中心到期的租户失败：%s", exc.message)
                return 0
            started = 0
            for t in expired:
                try:
                    async with s.begin_nested():
                        await ops._start(UUID(t["id"]), t["code"], "SUSPEND", who, {}, "到期")
                    started += 1
                except Conflict:
                    continue  # 有进行中的操作：下一轮再看
            await s.commit()
            return started
