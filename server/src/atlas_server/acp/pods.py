"""Pod 供给的接缝（acp 详设 §08）。

步骤 5 的 PodManager 实现它（K8s 模板、配对 Secret、NetworkPolicy）。
步骤 4 用假实现指向一个本地起的 bridge —— 端到端链路因此不依赖 K8s。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..db.models import Thread
    from ..domain.spec import CliSpec

__all__ = ["PodEndpoint", "PodProvider"]


class PodEndpoint(NamedTuple):
    """连上一个会话 Pod 需要的全部信息。

    ★ token 随 Pod 生灭：Pod 销毁即失效，重建换新（执行环境 §09）。
      所以它跟着「这次拿到的 Pod」一起返回，而不是从配置里读一个长期值。
    """

    url: str
    token: str


@runtime_checkable
class PodProvider(Protocol):
    async def ensure(self, thread: Thread, cli: CliSpec) -> PodEndpoint:
        """确保该会话的 Pod 在跑，返回连接信息。

        ★ cli 走参数而不是构造器：provider 是**进程级**的，而镜像与 adapter
          来自这一轮的 agent 版本快照 —— 绑在实例上就等于全进程共用一个
          镜像，换 agent 时静默用错版本。
        ★ 幂等：同一个 thread 反复调应当复用同一个 Pod（按 K8s label 查，
          不建 pod 表 —— 两处真相必然漂移）。
        """
        ...
