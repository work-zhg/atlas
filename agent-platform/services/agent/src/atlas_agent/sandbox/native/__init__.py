"""沙箱 · 原生沙箱（c4-model/agent/sandbox/code-sandbox-native.html）：原生智能体的 Shell 沙箱。

按工作区要沙箱（子智能体和父会话共用一个）；换算成通用供给的业务：类型 NATIVE、业务 id 是根会话的
uuid、人是会话的创建人。沙箱归人，由通用供给管理；strict 和评测两档各对应一个模板。

    WorkspaceRef          一个工作区：根会话 + 人
    ImagePolicy           镜像准入：空镜像换成默认，按档位只放行各自的前缀
    NativeSandboxKind     原生沙箱这一种（SandboxKind）：档位选模板、挂工作区和技能、
                          就绪看 execd /ping
    NativeSandboxService  ensure · current · confirm_gone · profile_of · reap_idle · release ·
                          reap_orphans
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from ...domain.spec import SandboxProfile, SandboxSpec
from ...domain.workspace_layout import SKILLS_MOUNT, WORKSPACE_MOUNT, Area, WorkspaceLayout
from ..base.execution.ports import SandboxExec
from ..base.lifecycle.spec import CreateSpec, Labels, Resources, Volume
from ..base.network.policy import NetworkPolicy
from ..provisioning.model import BizRef, BizType, SandboxRef
from ..provisioning.service import SandboxProvisioning

if TYPE_CHECKING:
    from ...config import Settings

__all__ = [
    "BENCH_TEMPLATE",
    "STRICT_TEMPLATE",
    "ImageDenied",
    "ImagePolicy",
    "NativeSandboxKind",
    "NativeSandboxService",
    "WorkspaceRef",
]

logger = logging.getLogger(__name__)

#: 档位就是模板：strict（非 root、根只读）、bench（评测：root、根可写）
STRICT_TEMPLATE = "sbx-strict"
BENCH_TEMPLATE = "bench"
_TEMPLATES: dict[str, SandboxProfile] = {STRICT_TEMPLATE: "strict", BENCH_TEMPLATE: "bench"}


def template_of(profile: SandboxProfile) -> str:
    return BENCH_TEMPLATE if profile == "bench" else STRICT_TEMPLATE


class ImageDenied(PermissionError):
    pass


@dataclass(frozen=True)
class WorkspaceRef:
    """一个工作区：所属的根会话加上人（会话的创建人，和对象存储路径 {user}/… 里的是同一个值）。"""

    root_thread_uuid: UUID
    user_uuid: UUID

    def biz(self) -> BizRef:
        return BizRef(BizType.NATIVE, self.root_thread_uuid, self.user_uuid)

    def layout(self) -> WorkspaceLayout:
        return WorkspaceLayout.of(self.user_uuid, self.root_thread_uuid)

    @classmethod
    def of_biz(cls, biz: BizRef) -> WorkspaceRef:
        return cls(biz.biz_id, biz.user_uuid)


#: 按档位的镜像前缀白名单
AllowList = dict[str, list[str]]


class ImagePolicy:
    """镜像准入。★ 任意镜像 = 任意代码以会话身份跑在集群里；评测档是 root，只该跑评测题的镜像。

    白名单可以是固定的，也可以每次准入时现取（运行期配置 sandbox.image_allow_prefixes，
    runtime_config.py）：管理员改了，下一个新建的沙箱就按新的判。平台默认镜像总是放行。
    """

    def __init__(
        self,
        default_image: str,
        allow: AllowList | Callable[[], Awaitable[AllowList]],
        *,
        bench_available: bool,
    ) -> None:
        self._default = default_image
        self._allow = allow
        self._bench = bench_available

    def resolve(self, image: str) -> str:
        return image or self._default

    async def check(self, image: str, profile: SandboxProfile) -> None:
        if profile == "bench" and not self._bench:
            raise ImageDenied("这个部署没有评测档沙箱（未配置 OPENSANDBOX_BENCH_URL）")
        if image == self._default:
            return
        allow = self._allow if isinstance(self._allow, dict) else await self._allow()
        if not any(image.startswith(p) for p in allow.get(profile, [])):
            scope = "评测档的" if profile == "bench" else ""
            raise ImageDenied(f"沙箱镜像 {image!r} 不在{scope}允许范围内")


class NativeSandboxKind:
    """原生沙箱这一种。无状态：装配时按 biz_type = NATIVE 登记到通用供给，由它在确保沙箱时回调。

    ★ 出站放行公网，集群网段由平台底线（strict 档 egress 镜像的 deny.always）拒绝；评测档的
      lifecycle server 用不带底线的 egress（它的出网代理在集群内），另给评测代理（集群内地址
      不走代理）。
    """

    biz_type = BizType.NATIVE

    def __init__(self, settings: Settings, exec: SandboxExec, images: ImagePolicy) -> None:
        self._settings = settings
        self._exec = exec
        self._images = images

    async def admit(self, req: SandboxSpec) -> None:
        await self._images.check(self._images.resolve(req.image), req.profile)

    def build(self, req: SandboxSpec, biz: BizRef) -> CreateSpec:
        s = self._settings
        layout = WorkspaceRef.of_biz(biz).layout()
        env = {
            "EXECD_INIT": "1",  # execd 作 PID 1：回收僵尸进程、转发信号
            "EXECD_ENVS": "/tmp/.execd/env",  # 默认在 /opt/opensandbox，而 strict 档的根是只读的
            "TMPDIR": "/tmp",
        }
        if req.profile != "bench":
            # strict 档的 HOME 是模板挂的 emptyDir；评测档按镜像自己的
            # （题目环境可能在 HOME 里预置了东西）
            env["HOME"] = "/home/agent"
        elif proxy := s.sandbox_bench_proxy.get_secret_value():
            # curl、uv 认小写，有的工具只认大写：两套都给。集群内地址不走代理
            for name in ("http_proxy", "https_proxy"):
                env[name] = env[name.upper()] = proxy
            env["no_proxy"] = env["NO_PROXY"] = "localhost,127.0.0.1,::1,.svc,.cluster.local"
        return CreateSpec(
            template=template_of(req.profile),
            image=self._images.resolve(req.image),
            entrypoint=("tail", "-f", "/dev/null"),
            network=NetworkPolicy.allow_all(),
            resources=Resources(
                s.sandbox_cpu_request,
                s.sandbox_cpu_limit,
                s.sandbox_memory_request,
                s.sandbox_memory_limit,
            ),
            env=env,
            volumes=(
                Volume("workspace", s.sandbox_pvc, layout.prefix(Area.WORKSPACE), WORKSPACE_MOUNT),
                Volume(
                    "skills",
                    s.sandbox_pvc,
                    layout.prefix(Area.SKILLS),
                    SKILLS_MOUNT,
                    read_only=True,
                ),
            ),
            labels=Labels.of(user=str(biz.user_uuid)),
        )

    async def attach(self, ref: SandboxRef, req: SandboxSpec, *, created: bool) -> None:
        return None

    async def ready(self, ref: SandboxRef) -> None:
        """execd 的 /ping 回话才算就绪。

        沙箱报 Running 时 execd 可能还没起来（带 egress 时晚约 1 秒）。
        """
        deadline = time.monotonic() + self._settings.opensandbox_timeout_s
        while not await self._exec.ping(ref.sandbox_uuid):
            if time.monotonic() > deadline:
                raise TimeoutError(f"沙箱 {ref.sandbox_uuid} 的 execd 没有就绪")
            await asyncio.sleep(0.5)


class NativeSandboxService:
    """原生沙箱的入口。闲置回收、删除级联、孤儿回收都只释放自己的业务，沙箱退不退役由通用供给决定。"""

    def __init__(
        self, provisioning: SandboxProvisioning, exec: SandboxExec, *, idle_ttl_s: float
    ) -> None:
        self._prov = provisioning
        self._exec = exec
        self._idle_ttl_s = idle_ttl_s

    async def ensure(self, ws: WorkspaceRef, spec: SandboxSpec) -> SandboxRef:
        """已有的原样复用：改了镜像或档位，要等闲置回收后才按新配置建。"""
        return await self._prov.ensure(ws.biz(), spec)

    async def current(self, ws: WorkspaceRef) -> SandboxRef | None:
        """通用供给标为 READY 的沙箱；没有就走对象存储，不会触发新建。"""
        return await self._prov.current(ws.biz())

    async def confirm_gone(self, ws: WorkspaceRef, ref: SandboxRef) -> bool:
        return await self._prov.confirm_gone(ws.biz(), ref)

    async def profile_of(self, ref: SandboxRef) -> SandboxProfile | None:
        """按通用供给登记的模板 code 换算档位；沙箱已经不在时返回 None。"""
        described = await self._prov.describe(ref)
        if described is None:
            return None
        return _TEMPLATES.get(described.template_code, "strict")

    async def reap_idle(self) -> int:
        """没有命令在跑、且闲置超过时限：经通用供给释放这个工作区（reason=idle）。

        ★ 命令是不是真的还在跑，由基座执行逐个向 execd 确认；问不到就当它还在跑。
        """
        now = datetime.now(UTC)
        released = 0
        for biz, ref in await self._prov.serving(BizType.NATIVE):
            try:
                activity = await self._exec.activity(ref.sandbox_uuid)
            except Exception:
                logger.warning("查沙箱 %s 的活动失败，这次不回收", ref.sandbox_uuid, exc_info=True)
                continue
            if activity.busy() or activity.idle_for(now) < self._idle_ttl_s:
                continue
            logger.info("工作区 %s 的沙箱 %s 闲置，释放", biz.biz_id, ref.sandbox_uuid)
            await self._prov.release(biz, reason="idle")
            released += 1
        return released

    async def release(self, ws: WorkspaceRef) -> None:
        """会话删除级联（reason=release）。失败只记日志。"""
        await self._prov.release(ws.biz(), reason="release")

    async def reap_orphans(self, live_ws: Iterable[UUID]) -> int:
        """所属会话已不在的工作区，一律释放（reason=orphan）。"""
        return await self._prov.reap_orphans(BizType.NATIVE, live_ws)
