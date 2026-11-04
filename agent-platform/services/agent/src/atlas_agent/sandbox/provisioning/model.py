"""通用供给的领域对象（c4-model/agent/sandbox/code-sandbox-provisioning.html）。

BizType · BizRef        要用沙箱的一个业务：类型 + 业务 id + 人
SandboxRef              交给调用方的沙箱句柄：只有 sandbox_uuid
SandboxStatus           供给自己的状态（CREATING / READY / RETIRING），不是 OpenSandbox 的运行状态
SandboxBinding          沙箱正在服务的一个业务（按业务全局唯一，唯一约束兜底）
UserSandbox             这个人名下的一个沙箱，和基座的实例一对一
SandboxAccount          一个人的沙箱账户：聚合根，保存时用版本号 CAS
QuotaPolicy             每人同时几个沙箱（只数 CREATING 和 READY）
AllocationPolicy        给没绑定的业务挑沙箱：现在独占，一律返回 None（登记一个新的）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from ...ids import uuid7
from ..base.lifecycle.spec import CreateSpec

__all__ = [
    "AllocationPolicy",
    "BizRef",
    "BizType",
    "QuotaExceeded",
    "QuotaPolicy",
    "SandboxAccount",
    "SandboxBinding",
    "SandboxRef",
    "SandboxRetiring",
    "SandboxStatus",
    "UserSandbox",
]


class QuotaExceeded(RuntimeError):
    """这个人名下的沙箱已经到上限。"""


class SandboxRetiring(RuntimeError):
    """沙箱正在退役，不再接业务。"""


class BizType(StrEnum):
    """业务类型。新增一种要用沙箱的业务，就加一个取值，再实现一个 SandboxKind。"""

    NATIVE = "native"
    CLI_AGENT = "cli_agent"


@dataclass(frozen=True)
class BizRef:
    """要用沙箱的一个业务。

    原生沙箱的业务 id 是工作区所属根会话的 uuid（子智能体与父会话共用）；CLI Agent 的是会话的 uuid。
    """

    biz_type: BizType
    biz_id: UUID
    user_uuid: UUID


@dataclass(frozen=True)
class SandboxRef:
    """沙箱句柄，只有 sandbox_uuid。沙箱换了 uuid 就变，比较一下就知道环境重建过。"""

    sandbox_uuid: UUID


class SandboxStatus(StrEnum):
    CREATING = "creating"
    READY = "ready"
    RETIRING = "retiring"

    def serving(self) -> bool:
        """只有 READY 能交给业务。"""
        return self is SandboxStatus.READY

    def counted(self) -> bool:
        """计入配额。"""
        return self in (SandboxStatus.CREATING, SandboxStatus.READY)


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class SandboxBinding:
    biz: BizRef
    bound_at: datetime = field(default_factory=_now)


@dataclass
class UserSandbox:
    """模板和镜像建好后不变；现在一个沙箱只绑一个业务，以后共享时可以绑多个。"""

    sandbox_uuid: UUID
    user_uuid: UUID
    template_code: str
    image: str
    status: SandboxStatus = SandboxStatus.CREATING
    bindings: list[SandboxBinding] = field(default_factory=list)
    created_at: datetime = field(default_factory=_now)
    ready_at: datetime | None = None

    @property
    def ref(self) -> SandboxRef:
        return SandboxRef(self.sandbox_uuid)

    def bind(self, biz: BizRef) -> None:
        if self.status is SandboxStatus.RETIRING:
            raise SandboxRetiring(f"沙箱 {self.sandbox_uuid} 正在退役，不再接业务")
        if not any(b.biz == biz for b in self.bindings):
            self.bindings.append(SandboxBinding(biz))

    def unbind(self, biz: BizRef) -> None:
        self.bindings = [b for b in self.bindings if b.biz != biz]

    def serves(self, biz: BizRef) -> bool:
        return any(_same_biz(b.biz, biz) for b in self.bindings)

    def vacant(self) -> bool:
        return not self.bindings

    def mark_ready(self, at: datetime | None = None) -> None:
        if self.status is SandboxStatus.RETIRING:
            raise SandboxRetiring(f"沙箱 {self.sandbox_uuid} 正在退役")
        self.status, self.ready_at = SandboxStatus.READY, at or _now()

    def mark_retiring(self) -> None:
        self.status = SandboxStatus.RETIRING


def _same_biz(a: BizRef, b: BizRef) -> bool:
    """按业务类型 + 业务 id 判同一个业务（人由业务决定，不参与比较）。"""
    return a.biz_type == b.biz_type and a.biz_id == b.biz_id


class QuotaPolicy:
    """每个人能同时有几个沙箱：全局上限，原生沙箱和 CLI Agent 合计，只数 CREATING 和 READY。"""

    def __init__(self, default_limit: int) -> None:
        self.default_limit = default_limit

    def admit(self, account: SandboxAccount) -> None:
        count = sum(1 for s in account.sandboxes if s.status.counted())
        if count >= self.default_limit:
            raise QuotaExceeded(
                f"已有 {count} 个沙箱 / 会话 Pod（上限 {self.default_limit}），先关掉一些旧会话再试"
            )


class AllocationPolicy:
    """现在是独占：一律返回 None，登记一个新的。

    以后做共享只改这里：在同模板、同镜像、凭据集合相同、没在退役、没满的沙箱里挑一个。
    共享之前还要先解决同一个沙箱里各会话之间的文件隔离。
    """

    def pick(self, account: SandboxAccount, biz: BizRef, spec: CreateSpec) -> UserSandbox | None:
        return None


@dataclass
class SandboxAccount:
    """一个人的沙箱账户：名下的全部沙箱，是按人管理的一致性边界。

    配额、一个业务至多绑一个沙箱、退役中的沙箱不再接业务，这些规则都在这里判；保存时用版本号
    CAS，并发修改的一方重读后再判。
    """

    user_uuid: UUID
    version: int = 0
    sandboxes: list[UserSandbox] = field(default_factory=list)
    #: 库里还没有这个人的账户行（第一次给他分配沙箱时建）
    persisted: bool = False

    def get(self, sandbox_uuid: UUID) -> UserSandbox | None:
        return next((s for s in self.sandboxes if s.sandbox_uuid == sandbox_uuid), None)

    def bound(self, biz: BizRef) -> UserSandbox | None:
        return next((s for s in self.sandboxes if s.serves(biz)), None)

    def allocate(
        self,
        biz: BizRef,
        spec: CreateSpec,
        *,
        policy: AllocationPolicy,
        quota: QuotaPolicy,
    ) -> UserSandbox:
        """给业务分配一个沙箱：已绑定的原样返回；分配策略挑一个，或判过配额后登记一个新的（CREATING）。"""
        if (current := self.bound(biz)) is not None:
            return current
        chosen = policy.pick(self, biz, spec)
        if chosen is None:
            quota.admit(self)
            chosen = UserSandbox(
                sandbox_uuid=uuid7(),
                user_uuid=self.user_uuid,
                template_code=spec.template,
                image=spec.image,
            )
            self.sandboxes.append(chosen)
        chosen.bind(biz)
        return chosen

    def unbind(self, biz: BizRef) -> UserSandbox | None:
        """解绑，返回空出来的沙箱（没有业务了）；不再服务任何业务之外的情况返回 None。"""
        current = self.bound(biz)
        if current is None:
            return None
        current.bindings = [b for b in current.bindings if not _same_biz(b.biz, biz)]
        return current if current.vacant() else None

    def retire(self, sandbox_uuid: UUID) -> UserSandbox:
        sb = self.get(sandbox_uuid)
        if sb is None:
            raise LookupError(f"沙箱 {sandbox_uuid} 不在 {self.user_uuid} 名下")
        sb.mark_retiring()
        return sb

    def remove(self, sandbox_uuid: UUID) -> None:
        """基座删完之后：沙箱和它的绑定一起去掉（历史看基座的生命周期日志）。"""
        self.sandboxes = [s for s in self.sandboxes if s.sandbox_uuid != sandbox_uuid]
