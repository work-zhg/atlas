from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from atlas_engine.contracts import EngineError
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.requests import Request

from .api.v1 import router as v1_router
from .api.v1.previews import install_log_mask
from .config import get_settings
from .db.session import get_sessionmaker
from .errors import AppError
from .executor.inprocess import InProcessExecutor
from .host.runtime import HostRuntime
from .memory import make_memory, run_worker
from .providers.cluster import ClusterPods
from .redisx import make_redis
from .repositories.thread import ThreadRepository
from .stream.gate import StreamGate, StreamsBusy
from .telemetry import setup_telemetry, shutdown_telemetry

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """进程内执行器的生命周期（§12.1）。

    启动时回收孤儿 run：上次进程被杀时正在跑的 run 不会自己恢复，
    必须标成 interrupted，否则会话永远停在"运行中"。
    """
    settings = get_settings()
    # ★ 遥测要在任何 span 产生之前装好。装不上不拦着进程起（它是旁路），
    #   但会把失败喊出来 —— 否则就成了「以为在采集其实没有」。
    setup_telemetry(settings)
    # acp 的执行环境。Pod 生命周期归 cluster 服务 —— server 不碰 K8s。
    pods = ClusterPods(settings)
    # 跨会话记忆（记忆设计）。★ 没开就是 None —— 执行器据此完全跳过，
    #   不回落任何假实现。
    memory = make_memory(settings)
    # 管理 API 从 app.state 取同一个实例 —— 构造很贵（要加载
    # embedding 模型），不能每个请求新建一个。
    app.state.memory = memory
    app.state.executor = InProcessExecutor(
        get_sessionmaker(),
        settings,
        # acp agent 的执行：经会话 Pod 里的 bridge（上游协议 atlas.host.v1）
        acp_runtime=HostRuntime(get_sessionmaker(), settings, pods),
        memory=memory,
    )

    redis = make_redis(settings)
    try:
        async with get_sessionmaker()() as session:
            # ★ 启动时不标记孤儿 run（不再把 running 判成 interrupted）：
            #   进行中的 run 应当断联恢复，而不是直接判中断。
            # ★ 重启会丢掉「子 run 跑完时叫醒父 run」那个信号，所以启动时立刻补扫
            #   一次挂起的 run，不等周期到点。
            await app.state.executor.sweep_suspended()
            # Pod 孤儿：把**还活着的会话**交给 cluster，其余它自己清。
            # cluster 不认识 thread 表 —— 那条反向依赖不该有。
            live = await ThreadRepository(session).all_ids()
            await pods.reap([str(thread_id) for thread_id in live])
    except Exception:  # 回收失败不该阻止服务启动
        logging.getLogger(__name__).warning("孤儿回收失败", exc_info=True)
    finally:
        await redis.aclose()

    # 补唤醒的周期扫描。丢失的唤醒信号只有它能捞回来 —— 没有它，一个 run
    # 会永远停在「等待子智能体」而不报任何错。
    app.state.executor.start_sweeper()

    # 抽取 worker。★ 它自建 Redis 连接：lifespan 上面那个用完就关了。
    #   ★ 抽取跑在这里而不是 run 的收尾路径上 —— 它要调一次 LLM，同步做
    #     会把每一轮的收尾拖长（§11：记忆绝不能成为会话的硬依赖）。
    memory_task = None
    if memory is not None:
        worker_redis = make_redis(settings)
        memory_task = asyncio.create_task(
            run_worker(worker_redis, memory, max_attempts=settings.memory_max_attempts),
            name="memory-worker",
        )

    try:
        yield
    finally:
        if memory_task is not None:
            memory_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await memory_task
            with contextlib.suppress(Exception):
                await worker_redis.aclose()
        await app.state.executor.shutdown()
        # ★ executor 停完再 flush：最后那批 span 往往正是收尾阶段产生的，
        #   顺序反过来就会把它们丢在缓冲里。
        shutdown_telemetry()


def create_app() -> FastAPI:
    app = FastAPI(title="Atlas", version="0.1.0", lifespan=lifespan)

    # web 前端跨源直连（含 SSE）。allow_headers 必须显式列出自定义头 ——
    # 尤其 Last-Event-ID：断线续传靠它，漏了会静默退化成"每次重连都从头拉"。
    settings = get_settings()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        # Range：文件预览按前 1 MiB 取文本（doc/detail/file-preview.html §08）
        allow_headers=["Content-Type", "X-User-Id", "Idempotency-Key", "Last-Event-ID", "Range"],
    )
    install_log_mask()

    app.include_router(v1_router)

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

    return app


app = create_app()
