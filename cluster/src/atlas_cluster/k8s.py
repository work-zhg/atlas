"""KubernetesBackend —— 真集群实现。

★ `kubernetes-asyncio` 是**可选依赖**：没装就在构造时明确报错，而不是
  在第一次建 Pod 时才炸。宁可服务起不来，也不要起来一个连不上集群却
  假装正常的服务（执行环境 §09）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from atlas_cluster.backend import PodRef

logger = logging.getLogger(__name__)

__all__ = ["KubernetesBackend"]


class KubernetesBackend:
    """★ kubeconfig 路径**显式传或调用时读环境变量**，不依赖客户端库的默认值。

    `kubernetes_asyncio` 的默认路径是模块**导入时**冻结的常量
    （`os.environ.get("KUBECONFIG", "~/.kube/config")`）—— 进程启动后再设
    KUBECONFIG 就不生效了。集群内跑用 ServiceAccount 不受影响，但本机
    对着 kubeconfig 排查时会表现为「明明设了 KUBECONFIG 却说找不到 context」。
    """

    def __init__(self, namespace: str, *, kubeconfig: str | None = None) -> None:
        try:
            from kubernetes_asyncio import client, config  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - 取决于部署环境
            msg = (
                "KubernetesBackend 需要可选依赖 kubernetes-asyncio："
                "pip install 'atlas-cluster[k8s]'"
            )
            raise RuntimeError(msg) from exc
        self._namespace = namespace
        self._kubeconfig = kubeconfig
        self._client = client
        self._config = config
        self._api: Any = None

    async def _core(self) -> Any:
        if self._api is None:
            # 集群内用 ServiceAccount，集群外回落 kubeconfig（本地排查用）
            try:
                self._config.load_incluster_config()
            except Exception:  # noqa: BLE001 - 不在集群里
                import os  # noqa: PLC0415

                # 调用时读，不用库的导入期默认值（见类 docstring）
                path = self._kubeconfig or os.environ.get("KUBECONFIG") or None
                await self._config.load_kube_config(path)
            self._api = self._client.CoreV1Api()
        return self._api

    async def list_pods(self, *, label_selector: str) -> list[PodRef]:
        api = await self._core()
        resp = await api.list_namespaced_pod(self._namespace, label_selector=label_selector)
        return [
            PodRef(
                item.metadata.name,
                dict(item.metadata.labels or {}),
                phase=getattr(item.status, "phase", "Unknown"),
                # ★ delete 之后 Pod 还会在 list 里待一段（优雅终止），
                #   phase 仍是 Running，只是多了 deletionTimestamp。
                terminating=item.metadata.deletion_timestamp is not None,
                ip=getattr(item.status, "pod_ip", None) or "",
            )
            for item in resp.items
        ]

    async def create_pod(self, manifest: dict[str, Any]) -> PodRef:
        api = await self._core()
        try:
            created = await api.create_namespaced_pod(self._namespace, manifest)
        except Exception as exc:  # noqa: BLE001
            # ★ 409 要翻成 FileExistsError —— PodManager.ensure 用它识别
            #   「并发 ensure 撞上了」并转成复用。不翻的话那条分支在真集群上
            #   **永远走不到**（内存实现抛的就是 FileExistsError，所以单测全绿），
            #   并发建同一个会话会直接冒一个 ApiException 上去。
            #
            #   409 有两种：名字已被占用，以及旧 Pod 还在删（message 里写
            #   object is being deleted）。两种的处理都是「别建，去复用/等」。
            if getattr(exc, "status", None) == 409:
                msg = f"Pod {manifest['metadata']['name']} 已存在或正在删除"
                raise FileExistsError(msg) from exc
            raise
        # ★ 此刻没有 IP，容器也还没起 —— 要等就得用 wait_ready。
        return PodRef(created.metadata.name, dict(created.metadata.labels or {}))

    async def wait_ready(self, name: str, *, timeout_s: float) -> PodRef:
        api = await self._core()
        deadline = asyncio.get_running_loop().time() + timeout_s
        last = "未知"
        while asyncio.get_running_loop().time() < deadline:
            pod = await api.read_namespaced_pod(name, self._namespace)
            ready = any(
                c.type == "Ready" and c.status == "True" for c in (pod.status.conditions or [])
            )
            ip = getattr(pod.status, "pod_ip", None) or ""
            if ready and ip:
                return PodRef(
                    pod.metadata.name,
                    dict(pod.metadata.labels or {}),
                    phase=pod.status.phase,
                    ip=ip,
                )
            last = _why_not_ready(pod)
            await asyncio.sleep(0.5)

        # ★ 把"卡在哪"带上。光说超时的话，镜像拉不动、挂载失败、探针不过
        #   这三种完全不同的原因长得一模一样，而它们的处理方式各不相同。
        msg = f"Pod {name} 在 {timeout_s:.0f}s 内没有就绪：{last}"
        raise TimeoutError(msg)

    async def delete_pod(self, name: str) -> None:
        api = await self._core()
        try:
            await api.delete_namespaced_pod(name, self._namespace)
        except Exception as exc:  # noqa: BLE001
            # 幂等：已经没了不算失败。回收路径上抛异常会让后面的清理跳过。
            if getattr(exc, "status", None) != 404:
                raise

    async def apply_secret(self, manifest: dict[str, Any]) -> None:
        api = await self._core()
        name = manifest["metadata"]["name"]
        try:
            await api.create_namespaced_secret(self._namespace, manifest)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "status", None) != 409:
                raise
            await api.replace_namespaced_secret(name, self._namespace, manifest)

    async def delete_secret(self, name: str) -> None:
        api = await self._core()
        try:
            await api.delete_namespaced_secret(name, self._namespace)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "status", None) != 404:
                raise

    async def read_secret_value(self, name: str, key: str) -> str | None:
        """K8s 的 Secret 读回来是 base64 的 `data`（写进去的 `stringData`
        只是入参糖）—— 这里解回明文。"""
        import base64  # noqa: PLC0415

        api = await self._core()
        try:
            secret = await api.read_namespaced_secret(name, self._namespace)
        except Exception as exc:  # noqa: BLE001
            if getattr(exc, "status", None) == 404:
                return None
            raise
        raw = (secret.data or {}).get(key)
        if raw is None:
            return None
        return base64.b64decode(raw).decode("utf-8")

    async def aclose(self) -> None:
        """关掉 aiohttp 的连接池。

        ★ 长跑进程不关会泄漏连接 —— 真集群冒烟时 aiohttp 直接打了
          "Unclosed client session" 的警告。
        """
        if self._api is not None:
            await self._api.api_client.close()
            self._api = None


def _why_not_ready(pod: Any) -> str:
    """从 Pod 状态里挑出**可执行**的那句话。

    容器状态比 phase 具体得多：ImagePullBackOff 要去看镜像名和拉取凭据，
    CreateContainerConfigError 通常是 Secret 缺键，而挂载失败连容器状态都
    还没有 —— 只能从 phase 说起。
    """
    statuses = list(pod.status.init_container_statuses or []) + list(
        pod.status.container_statuses or []
    )
    for status in statuses:
        waiting = status.state.waiting if status.state else None
        if waiting is not None and waiting.reason:
            detail = f"：{waiting.message}" if waiting.message else ""
            return f"容器 {status.name} 处于 {waiting.reason}{detail}"[:300]
        terminated = status.state.terminated if status.state else None
        if terminated is not None and terminated.exit_code:
            return f"容器 {status.name} 已退出（code={terminated.exit_code}, {terminated.reason}）"
    return f"phase={pod.status.phase}"
