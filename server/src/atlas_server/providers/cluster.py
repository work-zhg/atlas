"""cluster 服务的客户端 —— PodProvider 的生产实现。

server **不自己碰 K8s**：模板、配额、凭证、回收都在 cluster 服务里
（它是全仓唯一持有 K8s 写权限的进程）。这里只是一个 HTTP 客户端。

★ 步骤 4 的 LocalPods 与它是同一个协议的两个实现，AcpRuntime 一行不改 ——
  接缝（acp/pods.py::PodProvider）当初留对了。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx
from atlas_cluster.schemas import EnsurePodRequest, PodInfo, ReapRequest

from ..acp.pods import PodEndpoint

if TYPE_CHECKING:
    from ..config import Settings
    from ..db.models import Thread
    from ..domain.spec import CliSpec

logger = logging.getLogger(__name__)

__all__ = ["ClusterPods", "ClusterUnavailable"]


class ClusterUnavailable(RuntimeError):
    """cluster 服务不可达或拒绝了请求。

    ★ 冒泡成 run.failed（AcpRuntime 会翻成 pod_unavailable）而不是静默
      重试：Pod 起不来的原因往往是配额或镜像，重试一百次也一样，
      而用户看到「转圈」比看到「配额满了，先关掉几个会话」糟得多。
    """


class ClusterPods:
    """一个进程一个。httpx 客户端复用连接池。"""

    def __init__(self, settings: Settings) -> None:
        self._base_url = str(settings.cluster_base_url).rstrip("/")
        self._timeout = settings.cluster_timeout_s

    def _client(self) -> httpx.AsyncClient:
        """拆成工厂是为了可测：测试把传输换成 ASGI 直连真 cluster app，
        于是请求体、状态码映射、错误措辞都测的是真的，只省掉一个端口。"""
        return httpx.AsyncClient(timeout=self._timeout)

    async def ensure(self, thread: Thread, cli: CliSpec) -> PodEndpoint:
        if not cli.image:
            msg = "acp agent 的 cli.image 为空，无法创建 Pod"
            raise ClusterUnavailable(msg)

        payload = EnsurePodRequest(
            thread_id=str(thread.id),
            user_id=str(thread.created_by),
            # ★ 两个 thread id 都要：workspace 挂父会话的前缀，skills 挂自己的。
            #   少传一个就只能由 cluster 猜，而猜错的表现是子智能体读不到父的
            #   产物、或者读到了父的技能（subagent §03）。
            workspace_thread_id=str(thread.workspace_thread_id),
            image=cli.image,
            adapter=cli.adapter,
            cli_type=cli.cli_type,
        )
        info = await self._post("/v1/pods/ensure", payload.model_dump(), PodInfo)
        if info.created:
            logger.info("cluster 为会话 %s 新建了 Pod %s", thread.id, info.pod_name)
        return PodEndpoint(url=info.url, token=info.token)

    async def release(self, thread_id: str) -> None:
        """会话删除的级联。失败只记日志 —— 不该挡住会话删除，孤儿由 reap 兜底。"""
        try:
            async with self._client() as client:
                await client.delete(f"{self._base_url}/v1/pods/{thread_id}")
        except Exception:
            logger.warning("cluster 级联清理失败 thread=%s", thread_id, exc_info=True)

    async def reap(self, live_thread_ids: list[str]) -> None:
        """启动时的孤儿回收。输入是**还活着的会话**（cluster 不认识 thread 表）。"""
        payload = ReapRequest(live_thread_ids=live_thread_ids)
        try:
            async with self._client() as client:
                await client.post(f"{self._base_url}/v1/pods/reap", json=payload.model_dump())
        except Exception:
            logger.warning("cluster 孤儿回收失败", exc_info=True)

    async def _post(self, path: str, payload: dict, model: type[PodInfo]) -> PodInfo:
        try:
            async with self._client() as client:
                resp = await client.post(f"{self._base_url}{path}", json=payload)
        except httpx.HTTPError as exc:
            msg = f"cluster 服务不可达（{self._base_url}）：{exc}"
            raise ClusterUnavailable(msg) from exc

        if resp.status_code == 429:
            # 配额 —— 原样把 cluster 的话带给用户，它已经写明了怎么办
            raise ClusterUnavailable(_detail(resp))
        if resp.status_code >= 400:
            msg = f"cluster 返回 {resp.status_code}：{_detail(resp)}"
            raise ClusterUnavailable(msg)
        return model.model_validate(resp.json())


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300]
    return str(body.get("detail", body))[:300]
