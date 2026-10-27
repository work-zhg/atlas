"""Pod 供给的接缝：HostRuntime 通过它拿到会话 Pod 里 bridge 的地址与凭据。

生产实现是 providers/opensandbox/acp.py 的 OpenSandboxPods
（经 OpenSandbox 的 opensandbox-acp 建 Pod）；
测试用假实现直接指向一个本地起的 bridge，端到端链路因此不依赖 K8s。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..db.models import Thread
    from ..domain.spec import CliSpec

__all__ = ["AcpModel", "PodEndpoint", "PodProvider"]


class PodEndpoint(NamedTuple):
    """连上一个会话 Pod 需要的全部信息。

    ★ token 随 Pod 生灭：Pod 销毁即失效，重建换新（执行环境 §09）。
      所以它跟着「这次拿到的 Pod」一起返回，而不是从配置里读一个长期值。
    """

    url: str
    token: str


class AcpModel(NamedTuple):
    """经模型网关时交给 Pod 里 CLI 的模型接入（doc/model-design.html §8.1）。

    ★ 只有网关地址、这一轮权限主体的 Key 和模型名：供应商 Key 不进 Pod。
    """

    base_url: str
    key: str
    model: str
    #: CLI 做标题这类旁路工作的轻量模型：也要指向主体有权的模型
    small_model: str
    #: 只作用量归属参考的请求头（会话级：使用者、Agent）
    headers: dict[str, str]


@runtime_checkable
class PodProvider(Protocol):
    async def ensure(
        self, thread: Thread, cli: CliSpec, *, model: AcpModel | None = None
    ) -> PodEndpoint:
        """确保该会话的 Pod 在跑，返回连接信息。

        ★ cli 走参数而不是构造器：provider 是**进程级**的，而镜像与 adapter
          来自这一轮的 agent 版本快照 —— 绑在实例上就等于全进程共用一个
          镜像，换 agent 时静默用错版本。
        ★ 幂等：同一个 thread 反复调应当复用同一个 Pod（按沙箱的 metadata 查，
          不建 pod 表 —— 两处真相必然漂移）。
        ★ model：经模型网关时的模型接入，只在新建 Pod 时下发（会话级快照，§8.1）；
          None = 没接模型网关，照旧按部署配置。
        """
        ...
