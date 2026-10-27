from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from atlas_engine.contracts import EngineError
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.requests import Request

from .api.internal import router as internal_router
from .api.v1 import router as v1_router
from .api.v1.previews import install_log_mask
from .auth.service import listen_invalidations
from .auth.uc import UCClient
from .config import get_settings
from .db.session import get_sessionmaker
from .errors import AppError
from .executor.inprocess import InProcessExecutor
from .host.runtime import HostRuntime
from .instance import instance_id
from .maintainer import Maintainer
from .memory import Backfiller, make_memory_runtime
from .outbound import IdentityScopeMiddleware
from .providers.opensandbox import OpenSandboxPods
from .redisx import make_redis
from .stream.gate import StreamGate, StreamsBusy
from .telemetry import setup_telemetry, shutdown_telemetry

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _on_sigterm(start_drain: Callable[[], None]) -> None:
    """收到 SIGTERM 就开始停机交接（横向扩展设计 §6），再交给 uvicorn 原来的处理。

    ★ 为什么不能等 lifespan 的 shutdown：uvicorn 收到信号后先停止接新连接，然后
      **一直等进行中的响应结束**才走 lifespan shutdown —— 而会话 SSE 永不自己结束。
      不在信号到达的那一刻就开始交接、关 SSE，进程只会被 K8s 强杀，run 走最慢的
      租约过期接管。
    ★ 只在主线程装得上（signal 的限制）：测试里 lifespan 跑在别的线程，跳过即可。
    """
    loop = asyncio.get_running_loop()
    previous = signal.getsignal(signal.SIGTERM)

    def handler(signum: int, frame: object) -> None:
        loop.call_soon_threadsafe(start_drain)
        if callable(previous):
            previous(signum, frame)

    with contextlib.suppress(ValueError):
        signal.signal(signal.SIGTERM, handler)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """执行器、维护者与各后台任务的生命周期（横向扩展设计 §3 · §6 · §7）。

    ★ 启动时不再做「本进程专属」的回收：在途 run 由任何执行器按租约接管，
      孤儿 Pod 由维护者（全局一个）定期回收 —— 多副本下每个副本启动各跑一遍
      reap 会与别的副本正在建的 Pod 竞态。
    """
    settings = get_settings()
    executes = settings.server_role != "api"
    logger.info("atlas-server 启动：角色=%s 实例=%s", settings.server_role, instance_id())
    # ★ 遥测要在任何 span 产生之前装好。装不上不拦着进程起（它是旁路），
    #   但会把失败喊出来 —— 否则就成了「以为在采集其实没有」。
    setup_telemetry(settings)
    # acp 的执行环境：OpenSandbox 的 opensandbox-acp 建 Pod
    # （doc/sandbox-opensandbox-design.html §11）。
    # server 自己仍不碰 K8s。★ 进程级的 Redis 客户端：ensure 的锁与令牌在 Redis
    pods_redis = make_redis(settings)
    pods = OpenSandboxPods(settings, pods_redis)
    # 跨会话记忆（doc/memory-design.html）。★ 没接入记忆服务就是 None —— 执行器、管理接口、
    #   补记据此完全跳过，不回落任何假实现。
    memory = make_memory_runtime(settings, get_sessionmaker())
    app.state.memory = memory
    app.state.executor = InProcessExecutor(
        get_sessionmaker(),
        settings,
        # acp agent 的执行：经会话 Pod 里的 bridge（上游协议 atlas.host.v1）
        acp_runtime=HostRuntime(get_sessionmaker(), settings, pods),
        memory=memory,
    )

    executor = app.state.executor
    if executes:
        try:
            # ★ 重启会丢掉「子 run 跑完时叫醒父 run」那个信号，所以启动时立刻补扫
            #   一次挂起的 run，不等周期到点。
            await executor.sweep_suspended()
        except Exception:  # 补扫失败不该阻止服务启动：周期扫描还会来
            logger.warning("启动补扫失败", exc_info=True)
        # 领取扫描、唤醒订阅、补唤醒扫描。排队的 run 与租约过期的 run 都从这里被领走
        executor.start()

    # ★ 停机交接：信号到达即开始（见 _on_sigterm），lifespan 的 shutdown 只是等它做完
    gate: StreamGate = app.state.stream_gate

    def start_drain() -> None:
        if getattr(app.state, "draining", False):
            return
        app.state.draining = True
        logger.info("收到停机信号：停止领取、关闭事件流、交接在跑的 run")
        gate.drain()
        app.state.drain_task = asyncio.create_task(executor.drain(), name="drain")

    app.state.draining = False
    _on_sigterm(start_drain)

    background: list[asyncio.Task[None]] = [
        # 授权快照失效广播（§7.3）：别的实例撤销了用户，本实例清缓存
        asyncio.create_task(listen_invalidations(settings), name="authz-invalidations"),
    ]
    maintainer = None
    if executes:
        # 全局单例任务（Pod 回收、会话后补记）：所有执行器竞选，一个在做（§7.1）
        maintainer = Maintainer(
            settings,
            get_sessionmaker(),
            backfiller=(
                Backfiller(memory, executor.model_builder, lambda: make_redis(settings))
                if memory is not None
                else None
            ),
        )
        background.append(asyncio.create_task(maintainer.run_forever(), name="maintainer"))

    try:
        yield
    finally:
        start_drain()  # 没收到 SIGTERM 的停机（本机 Ctrl-C、测试）也走交接
        await executor.shutdown()
        for task in background:
            task.cancel()
        for task in background:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        with contextlib.suppress(Exception):
            await pods_redis.aclose()
        if maintainer is not None:
            await maintainer.stop()
            await maintainer.release()
        await app.state.uc.aclose()
        # ★ executor 停完再 flush：最后那批 span 往往正是收尾阶段产生的，
        #   顺序反过来就会把它们丢在缓冲里。
        shutdown_telemetry()


def create_app() -> FastAPI:
    app = FastAPI(title="Atlas", version="0.1.0", lifespan=lifespan)
    # 用户中心客户端（权限设计 §4）：每个请求按 X-Auth-Uid 取授权快照；内网免令牌，不需要应用凭据。
    # ★ 建在这里而不是 lifespan：app 一建出来鉴权就可用（lifespan 结束时关闭）
    app.state.uc = UCClient(get_settings())

    # web 前端跨源直连（含 SSE）。allow_headers 必须显式列出自定义头 ——
    # 尤其 Last-Event-ID：断线续传靠它，漏了会静默退化成"每次重连都从头拉"。
    settings = get_settings()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        # Range：文件预览按前 1 MiB 取文本（doc/detail/file-preview.html §08）
        allow_headers=[
            "Content-Type",
            "X-Uid",
            "Idempotency-Key",
            "Last-Event-ID",
            "Range",
        ],
    )
    # 每个请求一个干净的出站身份（主体解析时设置，见 outbound.py）
    app.add_middleware(IdentityScopeMiddleware)
    install_log_mask()

    app.include_router(v1_router)
    app.include_router(internal_router)

    # ★ SSE 并发闸门。会话流的寿命是「用户开着这个会话多久」，而不是「一轮
    #   多久」—— 每条占一个 Redis 连接，不设限的表现是**所有** HTTP 请求一起
    #   变慢（池耗尽后新命令排队）。见 stream/gate.py。
    #
    #   放在 create_app 而不是 lifespan：它是进程级资源计数器，测试里直接
    #   construct app 也要能用。
    app.state.stream_gate = StreamGate(settings.sse_max_connections)

    @app.exception_handler(StreamsBusy)
    async def streams_busy_handler(_: Request, exc: StreamsBusy) -> JSONResponse:
        """503 + Retry-After。★ 明确拒绝，不排队等一个可能几小时才释放的槽位。"""
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": "5"},
            content={"error": {"kind": "streams_busy", "message": str(exc), "details": {}}},
        )

    @app.exception_handler(EngineError)
    async def engine_error_handler(_: Request, exc: EngineError) -> JSONResponse:
        """统一错误体（文档 §11）：{"error": {kind, message, details}}。

        engine 的 InvalidSpec 在这里变成 400 —— 编辑器点保存时就能看到
        "opus-5 不接受 temperature"，而不是等到某次 run 才炸（§3 D3）。
        """
        status = 503 if exc.retryable else 400
        return JSONResponse(
            status_code=status,
            content={
                "error": {
                    "kind": exc.kind,
                    "message": exc.message,
                    "details": {k: str(v) for k, v in exc.details.items()},
                }
            },
        )

    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "kind": exc.kind,
                    "message": exc.message,
                    "details": {k: str(v) for k, v in exc.details.items()},
                }
            },
        )

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> JSONResponse:
        """就绪探针：停机交接期间返回 503，负载均衡摘掉本实例（横向扩展设计 §6）。"""
        if getattr(request.app.state, "draining", False):
            return JSONResponse(status_code=503, content={"status": "draining"})
        return JSONResponse(content={"status": "ok"})

    return app


app = create_app()
