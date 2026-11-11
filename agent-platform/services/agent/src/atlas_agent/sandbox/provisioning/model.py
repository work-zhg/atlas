"""通用供给的领域对象（c4-model/agent/sandbox/code-sandbox-provisioning.html）。

BizType · BizRef        要用沙箱的一个业务：类型 + 业务 id + 人
SandboxRef              交给调用方的沙箱句柄：只有 sandbox_uuid
SandboxStatus           供给自己的状态（CREATING / READY / RETIRING），不是 OpenSandbox 的运行状态
SandboxBinding          沙箱正在服务的一个业务（按业务全局唯一，唯一约束兜底），带沙箱内的槽位号
UserSandbox             这个人名下的一个沙箱，和基座的实例一对一
SandboxAccount          一个人的沙箱账户：聚合根，保存时用版本号 CAS
Sharing                 一个业务怎么共享沙箱：共享键相同的业务共用，一个沙箱至多几个业务
QuotaPolicy             每人同时几个沙箱（只数 CREATING 和 READY）
AllocationPolicy        给没绑定的业务挑沙箱：共享键相同、没在退役、没满的里挑一个；独占的一律新建
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
    "Sharing",
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
    #: 沙箱内的槽位号：同一个沙箱里的业务各不相同，从 0 起取最小的空位。CLI Agent 按它分端口
    slot: int = 0


@dataclass(frozen=True)
class Sharing:
    """一个业务怎么共享沙箱。

    key 由业务类型给（如凭据集合），供给再拼上业务类型、模板和镜像：这几样都相同才能共用。
    capacity：一个沙箱至多服务几个业务（None = 不限）。
    """

    key: str = ""
    capacity: int | None = None


@dataclass
class UserSandbox:
    """模板、镜像、共享键建好后不变；一个沙箱可以绑多个业务（共享键为 None 的独占）。"""

    sandbox_uuid: UUID
    user_uuid: UUID
    template_code: str
    image: str
    status: SandboxStatus = SandboxStatus.CREATING
    bindings: list[SandboxBinding] = field(default_factory=list)
    created_at: datetime = field(default_factory=_now)
    ready_at: datetime | None = None
    #: 共享键：相同的业务可以共用这个沙箱；None = 独占
    share_key: str | None = None

    @property
    def ref(self) -> SandboxRef:
        return SandboxRef(self.sandbox_uuid)

    def bind(self, biz: BizRef) -> None:
        if self.status is SandboxStatus.RETIRING:
            raise SandboxRetiring(f"沙箱 {self.sandbox_uuid} 正在退役，不再接业务")
        if not any(_same_biz(b.biz, biz) for b in self.bindings):
            taken = {b.slot for b in self.bindings}
            slot = next(i for i in range(len(taken) + 1) if i not in taken)
            self.bindings.append(SandboxBinding(biz, slot=slot))

    def unbind(self, biz: BizRef) -> None:
        self.bindings = [b for b in self.bindings if not _same_biz(b.biz, biz)]

    def serves(self, biz: BizRef) -> bool:
        return any(_same_biz(b.biz, biz) for b in self.bindings)

    def binding(self, biz: BizRef) -> SandboxBinding | None:
        return next((b for b in self.bindings if _same_biz(b.biz, biz)), None)

    def vacant(self) -> bool:
        return not self.bindings

    def accepts(self, share_key: str, capacity: int | None) -> bool:
        """能不能再接一个共享键为 share_key 的业务：键相同、没在退役、没满。"""
        return (
            self.share_key == share_key
            and self.status is not SandboxStatus.RETIRING
            and (capacity is None or len(self.bindings) < capacity)
        )

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
    """给没绑定的业务挑一个沙箱：共享键相同、没在退役、没满的里面，先挑已就绪的、再挑在建的。

    返回 None = 登记一个新的（独占的业务一律如此）。

    ★ 同一个沙箱里的会话之间不隔离（用户 2026-11-04 定的前提）：Shell 能读写别的会话的文件、看到
      别的会话的进程。共享键只保证「环境一样」：模板、镜像、网络和凭据。
    """

    def pick(
        self, account: SandboxAccount, share_key: str | None, capacity: int | None
    ) -> UserSandbox | None:
        if share_key is None:
            return None
        open_ = [s for s in account.sandboxes if s.accepts(share_key, capacity)]
        ready = [s for s in open_ if s.status is SandboxStatus.READY]
        return (ready or open_ or [None])[0]


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
        sharing: Sharing | None = None,
    ) -> UserSandbox:
        """给业务分配一个沙箱：已绑定的原样返回；分配策略挑一个，或判过配额后登记一个新的（CREATING）。

        sharing 为 None 的独占。
        """
        if (current := self.bound(biz)) is not None:
            return current
        key = share_key_of(biz, spec, sharing)
        chosen = policy.pick(self, key, sharing.capacity if sharing is not None else None)
        if chosen is None:
            quota.admit(self)
            chosen = UserSandbox(
                sandbox_uuid=uuid7(),
                user_uuid=self.user_uuid,
                template_code=spec.template,
                image=spec.image,
                share_key=key,
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


def share_key_of(biz: BizRef, spec: CreateSpec, sharing: Sharing | None) -> str | None:
    """完整的共享键：业务类型、模板、镜像都相同，业务类型给的键也相同，才能共用。"""
    if sharing is None:
        return None
    return "|".join((biz.biz_type.value, spec.template, spec.image, sharing.key))
