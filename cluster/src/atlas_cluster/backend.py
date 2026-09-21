"""ClusterBackend —— 「对集群做什么」的协议。

与 FilesystemProtocol 同款做法：协议在，真实现（k8s.py）与内存实现各一个。
本机开发与 CI 没有集群，InMemoryBackend 让模板渲染、配额、幂等、回收这些
**真正的逻辑**照样能被测到 —— 它们才是这个服务的主体，K8s 调用只是末端。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = ["ClusterBackend", "InMemoryBackend", "PodRef"]


class PodRef:
    """集群里的一个 Pod。只带调度决策要用的字段。"""

    __slots__ = ("ip", "labels", "name", "phase", "terminating")

    def __init__(
        self,
        name: str,
        labels: dict[str, str],
        phase: str = "Running",
        *,
        terminating: bool = False,
        ip: str = "",
    ) -> None:
        self.name = name
        self.labels = labels
        self.phase = phase
        #: Pod IP。★ 未调度完就是空的 —— server 要连的地址由它拼出来，
        #:   所以 ensure 必须等到它有值（见 PodManager.ensure）。
        self.ip = ip
        #: ★ 正在优雅终止。K8s 的 delete 是**异步**的：返回之后 Pod 仍在
        #:   list 里、phase 还是 Running，只是多了 deletionTimestamp。
        #:   不看这个字段就会把一个正在死的 Pod 当成可复用 —— 连上去才
        #:   发现它在关闭。真集群抓到过一次。
        self.terminating = terminating

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"PodRef({self.name!r}, phase={self.phase!r})"


@runtime_checkable
class ClusterBackend(Protocol):
    async def list_pods(self, *, label_selector: str) -> list[PodRef]: ...

    async def create_pod(self, manifest: dict[str, Any]) -> PodRef: ...

    async def delete_pod(self, name: str) -> None: ...

    async def wait_ready(self, name: str, *, timeout_s: float) -> PodRef:
        """等到 Pod 的容器全部就绪，返回带 IP 的引用。

        ★ 有这个方法是因为 create_pod 返回时 Pod 还在 ContainerCreating：
          没有 IP、bridge 没监听。把 URL 和 token 在那一刻交给 server，
          第一轮必然以"连不上 bridge"告终 —— 而 AcpChannel 的连接超时只有
          10s 且不重试。真集群冒烟第一下就撞出来了。

        超时抛 TimeoutError —— 起不来的原因通常是镜像拉不动或挂载失败，
        重试一百次也一样，要让它冒泡成用户看得懂的失败。
        """
        ...

    async def apply_secret(self, manifest: dict[str, Any]) -> None: ...

    async def delete_secret(self, name: str) -> None: ...

    async def read_secret_value(self, name: str, key: str) -> str | None:
        """读回 Secret 里的一个值。

        ★ 有这个方法是因为 ensure 复用既有 Pod 时**不能重发 token** ——
          Pod 里已经注入过了，换一个新的只会让它与 Pod 内那份对不上。
          早先的实现直接读 `backend.secrets`（内存实现独有的属性），
          在真集群上恒失败并静默重签 —— bridge 握手必然被拒。
        """
        ...


class InMemoryBackend:
    """内存实现。语义与 K8s 对齐的三处：

      · create 已存在的名字 → 冲突（K8s 是 409）
      · delete 不存在的名字 → **不报错**（幂等，与 K8s 的 404-ignore 一致）
      · label_selector 是 `k=v,k2=v2` 的与关系

    ★ 一处**刻意不对齐**、必须靠真集群测的：delete 在这里是立即生效的，
      而 K8s 是异步的优雅终止（Pod 会有一段 Terminating 时间仍在 list 里）。
      模拟它会把这个内存实现变成半个 K8s；相关行为由
      tests/test_cluster_k8s.py 对着真集群验（需要 k3s）。
    """

    def __init__(self) -> None:
        self.pods: dict[str, PodRef] = {}
        self.secrets: dict[str, dict[str, Any]] = {}

    async def list_pods(self, *, label_selector: str) -> list[PodRef]:
        wanted = _parse_selector(label_selector)
        return [
            pod
            for pod in self.pods.values()
            if all(pod.labels.get(k) == v for k, v in wanted.items())
        ]

    async def create_pod(self, manifest: dict[str, Any]) -> PodRef:
        name = manifest["metadata"]["name"]
        if name in self.pods:
            msg = f"Pod {name} 已存在"
            raise FileExistsError(msg)
        # 内存里立刻"就绪"并给个假 IP —— 真集群那段调度与拉镜像的时间
        # 由 tests/test_cluster_k8s.py 覆盖。
        pod = PodRef(name, dict(manifest["metadata"].get("labels") or {}), ip="10.0.0.1")
        self.pods[name] = pod
        return pod

    async def delete_pod(self, name: str) -> None:
        self.pods.pop(name, None)

    async def wait_ready(self, name: str, *, timeout_s: float) -> PodRef:
        pod = self.pods.get(name)
        if pod is None:
            msg = f"Pod {name} 不存在"
            raise TimeoutError(msg)
        return pod

    async def apply_secret(self, manifest: dict[str, Any]) -> None:
        self.secrets[manifest["metadata"]["name"]] = manifest

    async def delete_secret(self, name: str) -> None:
        self.secrets.pop(name, None)

    async def read_secret_value(self, name: str, key: str) -> str | None:
        secret = self.secrets.get(name)
        if not isinstance(secret, dict):
            return None
        value = (secret.get("stringData") or {}).get(key)
        return str(value) if value is not None else None


def _parse_selector(selector: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in selector.split(","):
        if "=" in part:
            key, _, value = part.partition("=")
            out[key.strip()] = value.strip()
    return out
