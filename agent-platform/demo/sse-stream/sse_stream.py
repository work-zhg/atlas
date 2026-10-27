"""SSE 事件流：断线续传、跨实例、停机交接的最小可运行 demo。

运行：  .venv/bin/python demo/sse-stream/sse_stream.py   （不需要 API key，约 4 秒）
说明：  demo/sse-stream/README.html（原理、时序图、动手实验）

场景：用户在一个会话里说了两句话。服务端有两个实例 A、B，共用一个 Redis 和一个 Postgres
（demo 里都用内存模拟），各自是一个真实的 HTTP 服务。浏览器的事件流连在 B 上，发消息的
POST 落在 A 上：

    第一轮  A 执行，事件经 Redis 中继到 B，再推给浏览器
            → 浏览器断网（处理到 #3），断网期间 A 继续产出 #4 #5 #6
            → 网络恢复，带 Last-Event-ID: 3 重连 → 归档补 #4 #5，Redis 接上 #6，之后实时
    Redis 重启（事件流和序号计数器都丢了）
    第二轮  序号从 DB 水位接着数 → 中途 B 停机：发 retry 后主动关流
            → 浏览器带游标重连到 A，接着收
    最后    新开一个标签页（没有游标）→ 只回放最近 3 条

★ 游标是会话内单调递增的 thread_seq，写在 SSE 帧的 id 里。三条纪律：
  序号由共享计数器分配；服务端按游标拼接「归档 → Redis」两段；浏览器只认比游标大的事件。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse, StreamingResponse

THREAD = "t1"
REDIS_MAXLEN = 2  # Redis 流只留最近几条。Atlas 是 10_000，调小是为了让「归档段」派上用场
REPLAY_WINDOW = 3  # 没有游标的首连只回放最近几条。Atlas 是 500
HEARTBEAT_S = 0.3  # 空闲多久发一次心跳。Atlas 是 15 秒
SSE_LIMIT = 10  # 每个实例最多同时服务几条事件流。Atlas 是 200
GRACEFUL_S = 2  # 停机时最多等几秒（相当于 K8s 的 terminationGracePeriodSeconds）
STEP_S = 0.05  # 假模型每产出一个事件停一下，让「实时推送」看得清
BACKOFF_S = [0.5, 1, 2, 4, 8]  # 重连退避，和前端 sse.ts 一样
NOT_ARCHIVED = {"message.delta"}  # 打字效果的增量不归档：回放时正文由 message.completed 一次给全
TERMINAL = {"run.finished", "run.failed", "run.cancelled"}
SSE_HEADERS = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}


# ---------------------------------------------------------------------------
# 外部服务（内存模拟）。真实系统里是 Redis 和 Postgres，所有实例共用。
# ---------------------------------------------------------------------------
class FakeRedis:
    """会话事件流（XADD / XRANGE / XREAD BLOCK）+ 会话序号计数器（INCR）。"""

    def __init__(self) -> None:
        self.streams: dict[str, list[tuple[int, dict]]] = {}  # thread -> [(entry_id, event)]
        self.counters: dict[str, int] = {}
        self._entry_id = 0  # 模拟 Redis 按时间生成的 entry id：重启后也只增不减
        self._changed = asyncio.Condition()

    def next_thread_seq(self, thread_id: str, *, floor: int) -> int:
        """分配会话序号。真实系统是一段 Lua：「不存在就用水位初始化」和「递增」之间不能有窗口。"""
        if thread_id not in self.counters:
            self.counters[thread_id] = floor
            start = self.counters[thread_id]
            print(f"  [Redis] 没有 {thread_id} 的序号计数器 → 从 {start} 接着数（DB 水位 {floor}）")
        self.counters[thread_id] += 1
        return self.counters[thread_id]

    async def xadd(self, thread_id: str, event: dict) -> None:
        self._entry_id += 1
        stream = self.streams.setdefault(thread_id, [])
        stream.append((self._entry_id, event))
        del stream[:-REDIS_MAXLEN]  # MAXLEN 裁剪：早期事件只剩归档里那一份
        async with self._changed:
            self._changed.notify_all()

    def xrange(self, thread_id: str) -> list[tuple[int, dict]]:
        return list(self.streams.get(thread_id, []))

    async def xread(self, thread_id: str, after_id: int, block_s: float) -> list[tuple[int, dict]]:
        """阻塞读 after_id 之后的条目；block_s 内没有新条目就返回空列表。"""

        def newer() -> list[tuple[int, dict]]:
            return [(i, e) for i, e in self.streams.get(thread_id, []) if i > after_id]

        async with self._changed:
            try:
                await asyncio.wait_for(self._changed.wait_for(newer), block_s)
            except TimeoutError:
                return []
        return newer()

    def restart(self) -> None:
        self.streams.clear()
        self.counters.clear()


class FakeArchive:
    """Postgres 的 run_event 表：主键 (thread_id, thread_seq)，delta 不归档。"""

    def __init__(self) -> None:
        self.rows: dict[str, dict[int, dict]] = {}

    def insert(self, thread_id: str, events: list[dict]) -> list[int]:
        rows = self.rows.setdefault(thread_id, {})
        kept = [e for e in events if e["type"] not in NOT_ARCHIVED]
        for e in kept:
            if e["thread_seq"] in rows:
                raise KeyError(f"主键冲突 ({thread_id}, {e['thread_seq']})")
            rows[e["thread_seq"]] = e
        return [e["thread_seq"] for e in kept]

    def after(self, thread_id: str, cursor: int) -> list[dict]:
        rows = self.rows.get(thread_id, {})
        return [rows[s] for s in sorted(rows) if s > cursor]

    def max_seq(self, thread_id: str) -> int:
        """DB 水位：Redis 的序号计数器丢了，从这里接着数。"""
        return max(self.rows.get(thread_id, {}), default=0)

    def window_floor(self, thread_id: str, keep: int) -> int:
        """没有游标时的起点：只回放最近 keep 条。"""
        seqs = sorted(self.rows.get(thread_id, {}))
        return seqs[-keep] - 1 if len(seqs) > keep else 0


# ---------------------------------------------------------------------------
# 假模型：按用户的话产出一串事件。PAUSE 表示「模型在想」，由 main() 放行。
# ---------------------------------------------------------------------------
PAUSE = None
SCRIPTS: dict[str, list[tuple[str, dict] | None]] = {
    "北京天气怎么样？": [
        ("run.started", {}),
        ("message.delta", {"text": "我查一下"}),
        ("message.delta", {"text": "天气。"}),
        ("tool.started", {"call_id": "c1", "name": "get_weather"}),
        ("tool.completed", {"call_id": "c1", "output": "晴，25 度"}),
        ("message.delta", {"text": "北京今天晴，"}),
        PAUSE,
        ("message.delta", {"text": "25 度。"}),
        ("message.completed", {"text": "我查一下天气。北京今天晴，25 度。"}),
        ("run.finished", {}),
    ],
    "谢谢": [
        ("run.started", {}),
        ("message.delta", {"text": "不客气，"}),
        PAUSE,
        ("message.delta", {"text": "还有什么要查的吗？"}),
        ("message.completed", {"text": "不客气，还有什么要查的吗？"}),
        ("run.finished", {}),
    ],
}


def to_frame(event: dict) -> str:
    """一个 SSE 帧：id 写游标（thread_seq），浏览器重连时原样放进 Last-Event-ID。"""
    return f"id: {event['thread_seq']}\nevent: {event['type']}\ndata: {json.dumps(event)}\n\n"


def seqs(events: list[dict]) -> str:
    return " ".join(f"#{e['thread_seq']}" for e in events) or "无"


# ---------------------------------------------------------------------------
# 一个 server 实例：FastAPI + uvicorn，跑在本进程的一个随机端口上
# ---------------------------------------------------------------------------
class Instance:
    def __init__(self, name: str, redis: FakeRedis, archive: FakeArchive) -> None:
        self.name = name
        self.redis = redis
        self.archive = archive
        self.active = 0  # 正在服务的事件流条数（闸门）
        self.draining = False
        self.pauses: dict[str, asyncio.Event] = {}  # run_id -> 「模型想好了」
        self._runs = 0
        self._executing: set[asyncio.Task] = set()  # 持有引用，否则任务可能被垃圾回收
        config = uvicorn.Config(
            self._build_app(),
            host="127.0.0.1",
            port=0,
            log_level="warning",
            lifespan="off",
            timeout_graceful_shutdown=GRACEFUL_S,
        )
        self.server = uvicorn.Server(config)
        self.url = ""

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.post("/threads/{thread_id}/runs", status_code=202)
        async def create_run(thread_id: str, body: dict[str, str]) -> dict[str, str]:
            # 创建与订阅分开：这里只把 run 交给执行器就返回，事件走 GET /events
            self._runs += 1
            run_id = f"r{self._runs}"
            print(f"  [{self.name}] POST /runs「{body['content']}」→ 202 run_id={run_id}")
            task = asyncio.create_task(self.execute(thread_id, run_id, body["content"]))
            self._executing.add(task)
            task.add_done_callback(self._executing.discard)
            return {"run_id": run_id}

        @app.get("/threads/{thread_id}/events")
        async def events(thread_id: str, last_event_id: str | None = Header(None)) -> Any:
            # ★ 闸门在路由里先查：这时还能返回 503。进了生成器，200 的响应头已经发出去了
            if self.draining or self.active >= SSE_LIMIT:
                print(f"  [{self.name}] 事件流已满或正在停机 → 503")
                busy = {"detail": "busy"}
                return JSONResponse(busy, status_code=503, headers={"Retry-After": "1"})
            cursor = int(last_event_id) if last_event_id and last_event_id.isdigit() else None
            return StreamingResponse(
                self.stream(thread_id, cursor), media_type="text/event-stream", headers=SSE_HEADERS
            )

        return app

    # ------------------------------------------------------------------ 执行器
    async def execute(self, thread_id: str, run_id: str, content: str) -> None:
        tag = f"[{self.name}·{run_id}]"
        floor = self.archive.max_seq(thread_id)  # DB 水位，每段查一次
        unarchived: list[dict] = []  # 已发布、还没归档的
        try:
            for step in SCRIPTS[content]:
                if step is PAUSE:
                    self.pauses[run_id] = asyncio.Event()
                    await self.pauses[run_id].wait()
                    continue
                type_, data = step
                seq = self.redis.next_thread_seq(thread_id, floor=floor)
                event = {"thread_seq": seq, "run_id": run_id, "type": type_, "data": data}
                unarchived.append(event)
                if type_ in TERMINAL:
                    # ★ 先落库再发布：浏览器看到 run.finished ⇒ DB 已是最终状态
                    saved = self.archive.insert(thread_id, unarchived)
                    print(f"  {tag} 落库（归档 {' '.join(f'#{s}' for s in saved)}）后再发布 #{seq}")
                    await self.redis.xadd(thread_id, event)
                    return
                await self.redis.xadd(thread_id, event)
                print(f"  {tag} 发布 #{seq} {type_}")
                if type_ == "tool.completed":  # 检查点：一批工具结果收齐就落库
                    saved = self.archive.insert(thread_id, unarchived)
                    print(f"  {tag} 检查点：归档 {' '.join(f'#{s}' for s in saved)}，delta 不归档")
                    unarchived = []
                await asyncio.sleep(STEP_S)
        except KeyError as exc:
            print(f"  {tag} 归档失败：{exc.args[0]}")

    # ------------------------------------------------------------------ 事件流
    async def stream(self, thread_id: str, cursor: int | None) -> AsyncIterator[str]:
        """一条会话的事件流：先从归档补到游标，再从 Redis 接上，之后实时推送。永不自己结束。"""
        self.active += 1
        try:
            header = f"Last-Event-ID={cursor}"
            if cursor is None:
                cursor = self.archive.window_floor(thread_id, REPLAY_WINDOW)
                header = f"没有游标，只回放最近 {REPLAY_WINDOW} 条（#{cursor} 之后）"

            # 两段接力，按 thread_seq 拼接：归档读到哪，Redis 就从哪之后接
            archived = self.archive.after(thread_id, cursor)
            if archived:
                cursor = archived[-1]["thread_seq"]
            last_id = 0
            from_redis: list[dict] = []
            for entry_id, event in self.redis.xrange(thread_id):
                last_id = entry_id
                if event["thread_seq"] > cursor:
                    from_redis.append(event)
            print(
                f"  [{self.name}] GET /events {header}"
                f" → 归档段：{seqs(archived)}；Redis 段：{seqs(from_redis)}；之后实时"
            )
            for event in archived + from_redis:
                yield to_frame(event)

            while True:
                entries = await self.redis.xread(thread_id, last_id, HEARTBEAT_S)
                if self.draining:
                    # ★ 停机时主动关：会话流永不自己结束，不关的话 uvicorn 会一直等它
                    print(f"  [{self.name}] 停机：给浏览器发 retry 后关闭事件流")
                    yield "retry: 1000\n\n"
                    return
                if not entries:
                    yield ": ping\n\n"  # 心跳：防止代理 / 负载均衡把空闲连接掐掉
                    continue
                for entry_id, event in entries:
                    last_id = entry_id
                    if event["thread_seq"] > cursor:
                        yield to_frame(event)
        finally:
            self.active -= 1

    # ------------------------------------------------------------------ 启停
    async def start(self) -> None:
        self._task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.01)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"

    async def shutdown(self) -> None:
        started = time.monotonic()
        self.draining = True
        self.server.should_exit = True
        await self._task
        print(f"  [{self.name}] 已停机，用时 {time.monotonic() - started:.1f} 秒")

    def resume(self, run_id: str) -> None:
        self.pauses.pop(run_id).set()


class LoadBalancer:
    """按顺序挑第一个没在停机的实例（相当于 K8s 把停机中的 Pod 摘出 Service）。"""

    def __init__(self, instances: list[Instance]) -> None:
        self.instances = instances

    def pick(self) -> Instance:
        return next(i for i in self.instances if not i.draining)


# ---------------------------------------------------------------------------
# 浏览器：照着前端 packages/chat-ui/src/lib/sse.ts 和 run-reducer.ts 写的
# ---------------------------------------------------------------------------
class NetworkDown(Exception):
    pass


class Browser:
    def __init__(self, name: str, lb: LoadBalancer) -> None:
        self.name = name
        self.lb = lb
        self.last_seq = 0  # 已处理的最大 thread_seq —— 重连时放进 Last-Event-ID
        self.turns: dict[str, dict[str, Any]] = {}  # run_id -> 这一轮在界面上的样子
        self.connected_to: str | None = None
        self.network = asyncio.Event()
        self.network.set()
        self.drop_after: int | None = None  # 处理完这个序号就「断网」
        self._pinged = False

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        attempt = 0
        async with httpx.AsyncClient(timeout=None) as http:
            while True:
                await self.network.wait()
                instance = self.lb.pick()
                headers = {"Accept": "text/event-stream"}
                if self.last_seq > 0:  # ★ 没有游标就不带：服务端给默认窗口，而不是全量回放
                    headers["Last-Event-ID"] = str(self.last_seq)
                url = f"{instance.url}/threads/{THREAD}/events"
                try:
                    async with http.stream("GET", url, headers=headers) as res:
                        if 400 <= res.status_code < 500:
                            print(f"  [{self.name}] {res.status_code}，不可恢复，不再重连")
                            return
                        if res.status_code != 200:
                            raise RuntimeError(
                                f"{res.status_code}，Retry-After={res.headers.get('retry-after')}"
                            )
                        attempt = 0
                        self.connected_to = instance.name
                        await self._read(res)
                    print(f"  [{self.name}] 连接被服务端关闭")
                except NetworkDown:
                    print(f"  [{self.name}] 网络断了（已处理到 #{self.last_seq}）")
                except (httpx.HTTPError, RuntimeError) as exc:
                    print(f"  [{self.name}] 连接 {instance.name} 失败：{exc}")
                self.connected_to = None
                # ★ 会话流不会正常结束，断了一律当意外：退避后带游标重连
                await asyncio.sleep(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
                attempt += 1

    async def _read(self, res: httpx.Response) -> None:
        buf = ""
        async for chunk in res.aiter_text():
            buf += chunk.replace("\r\n", "\n")
            while "\n\n" in buf:
                frame, buf = buf.split("\n\n", 1)
                self._on_frame(frame)

    def _on_frame(self, frame: str) -> None:
        if frame.startswith(":"):
            if not self._pinged:
                print(f"  [{self.name}] ← 心跳（{frame}），之后不再打印")
                self._pinged = True
            return
        if frame.startswith("retry:"):
            print(f"  [{self.name}] ← {frame}")
            return
        data = next(line[5:].strip() for line in frame.split("\n") if line.startswith("data:"))
        self._apply(json.loads(data))
        if self.last_seq == self.drop_after:
            self.drop_after = None
            self.network.clear()
            raise NetworkDown

    def _apply(self, event: dict) -> None:
        seq, type_, data = event["thread_seq"], event["type"], event["data"]
        # ★ 去重：重连边界可能重发，比游标小的一律丢弃（契约：thread_seq 会话内严格递增）
        if seq <= self.last_seq:
            print(f"  [{self.name}] 丢弃重复 #{seq} {type_}")
            return
        self.last_seq = seq
        brief = data.get("text") or data.get("name") or data.get("output") or ""
        print(f"  [{self.name}] ← #{seq} {type_} {brief}".rstrip())
        turn = self.turns.setdefault(event["run_id"], {"text": "", "tools": {}, "done": False})
        if type_ == "message.delta":
            turn["text"] += data["text"]
        elif type_ == "message.completed":
            turn["text"] = data["text"]  # 全文一次给全：漏掉的 delta 在这里补回来
        elif type_ == "tool.started":
            turn["tools"][data["call_id"]] = f"{data['name']} 运行中"
        elif type_ == "tool.completed" and data["call_id"] in turn["tools"]:
            turn["tools"][data["call_id"]] = turn["tools"][data["call_id"]].replace("运行中", "✓")
        elif type_ in TERMINAL:
            turn["done"] = True

    def show(self) -> None:
        print(f"  {self.name}（游标 #{self.last_seq}）")
        for run_id, turn in self.turns.items():
            tools = "、".join(turn["tools"].values()) or "无"
            state = "已结束" if turn["done"] else "进行中"
            print(f"    {run_id} [{state}] 工具：{tools}  正文：{turn['text']!r}")


# ---------------------------------------------------------------------------
# 剧本
# ---------------------------------------------------------------------------
async def until(check: Callable[[], bool], what: str, timeout: float = 3) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            print(f"  ⚠ 等了 {timeout:g} 秒也没等到：{what}")
            return
        await asyncio.sleep(0.01)


async def post(instance: Instance, content: str) -> str:
    async with httpx.AsyncClient() as http:
        res = await http.post(f"{instance.url}/threads/{THREAD}/runs", json={"content": content})
        return res.json()["run_id"]


async def main() -> None:
    redis, archive = FakeRedis(), FakeArchive()
    a, b = Instance("A", redis, archive), Instance("B", redis, archive)
    await a.start()
    await b.start()
    lb = LoadBalancer([b, a])  # 浏览器的事件流优先落到 B

    print("== 浏览器打开会话：事件流连到 B ==")
    tab = Browser("浏览器", lb)
    tab.start()
    await until(lambda: tab.connected_to == "B", "浏览器连上 B")

    print("\n== 第一轮：用户发消息，POST 落到 A；浏览器处理完 #3 时断网 ==")
    tab.drop_after = 3
    r1 = await post(a, "北京天气怎么样？")
    await until(lambda: r1 in a.pauses, "A 产出到 #6，模型开始思考")

    print("\n== 网络恢复 ==")
    tab.network.set()
    await until(lambda: tab.last_seq >= 6, "浏览器补齐到 #6")
    a.resume(r1)
    await until(lambda: tab.last_seq >= 9, "浏览器收到 #9")
    await asyncio.sleep(HEARTBEAT_S * 1.5)  # 一轮结束了，流还开着：空闲时只有心跳

    print("\n== Redis 重启：事件流和序号计数器都丢了 ==")
    redis.restart()

    print("\n== 第二轮：POST 落到 A；模型思考时 B 停机 ==")
    r2 = await post(a, "谢谢")
    await until(lambda: r2 in a.pauses and tab.last_seq >= 11, "浏览器收到 #11")
    await b.shutdown()
    await until(lambda: tab.connected_to == "A", "浏览器重连到 A")
    a.resume(r2)
    await until(lambda: tab.last_seq >= 14, "浏览器收到 #14")

    print("\n== 新开一个标签页（没有游标）==")
    tab2 = Browser("标签页2", lb)
    tab2.start()
    await until(lambda: tab2.last_seq >= 14, "标签页2 收到 #14")

    print("\n== 最终界面 ==")
    tab.show()
    tab2.show()
    await tab.close()
    await tab2.close()
    await a.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
