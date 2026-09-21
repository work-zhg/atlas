"""cluster 服务的 HTTP 面。

方法面刻意只有四个 —— 这是全仓唯一持有 K8s 写权限的进程，每多一个端点
就多一寸可以被误用的面。

    POST   /v1/pods/ensure    幂等地确保某会话的 Pod 在跑
    DELETE /v1/pods/{id}      删 Pod 与它的配对 Secret
    POST   /v1/pods/reap      按「活会话」名单清孤儿
    GET    /health            就绪探针
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from atlas_cluster.backend import ClusterBackend, InMemoryBackend
from atlas_cluster.config import ClusterSettings
from atlas_cluster.manager import PodManager, QuotaExceeded
from atlas_cluster.schemas import EnsurePodRequest, PodInfo, ReapRequest, ReapResult

logger = logging.getLogger(__name__)

__all__ = ["create_app"]


def create_app(
    *, backend: ClusterBackend | None = None, settings: ClusterSettings | None = None
) -> FastAPI:
    """backend 可注入 —— 测试用 InMemoryBackend，部署用 KubernetesBackend。

    ★ 默认是**内存实现**，不是自动连集群：一个连不上集群的服务默默起来
      并对每次 ensure 报错，比明确的「没配集群」难查得多。部署时由
      入口显式注入 KubernetesBackend（见 __main__）。
    """
    cfg = settings or ClusterSettings()
    manager = PodManager(backend or InMemoryBackend(), cfg)
    app = FastAPI(title="Atlas Cluster", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "namespace": cfg.namespace}

    @app.post("/v1/pods/ensure", response_model=PodInfo)
    async def ensure(req: EnsurePodRequest) -> PodInfo:
        try:
            return await manager.ensure(req)
        except QuotaExceeded as exc:
            # 429 而不是 500：调用方据此告诉用户「先关掉几个旧会话」，
            # 而 500 只会让它当作故障去重试，把配额撞得更死。
            raise HTTPException(status_code=429, detail=str(exc)) from exc

    @app.delete("/v1/pods/{thread_id}", status_code=204)
    async def release(thread_id: str) -> JSONResponse:
        await manager.release(thread_id)
        return JSONResponse(content=None, status_code=204)

    @app.post("/v1/pods/reap", response_model=ReapResult)
    async def reap(req: ReapRequest) -> ReapResult:
        return ReapResult(removed=await manager.reap(req.live_thread_ids))

    return app
