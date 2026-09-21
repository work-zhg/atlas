"""脚本化的假 ACP adapter —— 步骤 3 的联调对象。

真 CLI 要 K8s、要镜像、要 API key；本脚本在 stdio 上说同一种 JSON-RPC，
让 bridge 的全部本地事务（握手鉴权、单活跃 prompt、崩溃上报、权限转交）
在一台没有 Docker 的开发机上就能验证。

用法：作为子进程启动，行为由环境变量的剧本决定 ——

    ACP_FAKE_CRASH_ON=initialize   收到该方法后立刻非零退出（崩溃路径）
    ACP_FAKE_PROMPT_DELAY_S=0.5    prompt 响应前的停顿（用来制造"一轮在跑"）
    ACP_FAKE_ASK_PERMISSION=1      prompt 期间发一次 request_permission
    ACP_FAKE_BANNER=1              启动时往 stdout 打一行非 JSON（模仿真 CLI
                                   的启动横幅，验证 bridge 不会被它噎住）
    ACP_FAKE_NO_RESUME=1           声明不支持会话恢复（像 Codex 那类 adapter）——
                                   用来验证 lost 降级是**可见**的，不是静默新建
    ACP_FAKE_WRITE_FILE=notes.md   prompt 期间往 **cwd** 写一个文件，内容是
                                   prompt 的文本。用来验证 adapter 的 cwd 确实
                                   是挂进来的工作区 —— 挂错了的表现不是报错，
                                   是文件写进了容器本地、Pod 一没就不见了
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
from typing import Any

#: session/load 重放的历史正文。测试断言它**不出现**在新一轮的回答里。
REPLAYED_TEXT = "【上一轮的回答，不该出现在本轮】"


def _write(frame: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(frame, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _reply(frame_id: Any, result: Any) -> None:
    _write({"jsonrpc": "2.0", "id": frame_id, "result": result})


def _update(session_id: str, payload: dict[str, Any]) -> None:
    _write(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": session_id, "update": payload},
        }
    )


class FakeAdapter:
    def __init__(self) -> None:
        self.crash_on = os.environ.get("ACP_FAKE_CRASH_ON", "")
        self.prompt_delay_s = float(os.environ.get("ACP_FAKE_PROMPT_DELAY_S", "0"))
        self.ask_permission = os.environ.get("ACP_FAKE_ASK_PERMISSION") == "1"
        self.no_resume = os.environ.get("ACP_FAKE_NO_RESUME") == "1"
        self.write_file = os.environ.get("ACP_FAKE_WRITE_FILE", "")
        #: 记下收到过哪些方法 —— 测试据此断言走的是 load 还是 new。
        self.seen: list[str] = []
        self.session_id = "fake-session-1"
        self._next_id = 1000
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    async def run(self) -> None:
        if os.environ.get("ACP_FAKE_BANNER") == "1":
            # 真 CLI 常往 stdout 漏启动横幅 —— bridge 必须能跳过它继续。
            sys.stdout.write("fake-adapter v0 starting…\n")
            sys.stdout.flush()

        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)

        while True:
            line = await reader.readline()
            if not line:
                return
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            # ★ 每帧起任务：顺序 await 的话，处理 prompt 期间读不到
            #   权限响应 —— 与 bridge 的 WS 循环同一类阻塞，会死锁。
            self._tasks.add(task := asyncio.create_task(self._dispatch(frame)))
            task.add_done_callback(self._tasks.discard)

    async def _dispatch(self, frame: dict[str, Any]) -> None:
        method = frame.get("method")
        frame_id = frame.get("id")

        # 响应（bridge 回的权限决定）
        if method is None:
            future = self._pending.pop(frame_id, None)
            if future is not None and not future.done():
                future.set_result(frame.get("result"))
            return

        if method and method == self.crash_on:
            sys.stderr.write(f"fake adapter 故意崩在 {method}\n")
            sys.stderr.flush()
            os._exit(9)

        self.seen.append(str(method))

        if method == "initialize":
            # ★ 按真 adapter 的方式校验 params。
            #
            #   早先这里对 params 一眼不看，于是 AcpRuntime 一直传 {} 也照样
            #   通过 —— 直到接上真 CLI 才发现：真 adapter 按 schema 校验，
            #   缺 protocolVersion 就回 -32602，整轮以 runtime_unreachable 失败。
            #   假 adapter 不校验，等于把"我们发的参数对不对"这件事排除在
            #   测试之外，而那恰恰是最容易写错的部分。
            params = frame.get("params") or {}
            if not isinstance(params.get("protocolVersion"), int):
                _write(
                    {
                        "jsonrpc": "2.0",
                        "id": frame_id,
                        "error": {
                            "code": -32602,
                            "message": "Invalid params",
                            "data": {"protocolVersion": "必填，且为整数"},
                        },
                    }
                )
                return
            resumable = not self.no_resume
            # ★ 用**真 adapter 的形状**声明能力，不要自己发明一种。
            #
            #   claude-code-acp 实测返回的是
            #       "sessionCapabilities": {"fork": {}, "list": {}, "resume": {}}
            #   —— 字段名是 sessionCapabilities（不是 session），而且用
            #   「键存在」表示支持（值是空对象，不是 true）。
            #   这里原先发的是 {"session": {"resume": true}}，于是解析端把
            #   两处都写错了也照样全绿，直到接上真 CLI 才发现每轮都从头开始。
            session_caps: dict[str, Any] = {"fork": {}, "list": {}}
            if resumable:
                session_caps["resume"] = {}
            _reply(
                frame_id,
                {
                    "protocolVersion": 1,
                    "agentCapabilities": {
                        "loadSession": resumable,
                        "sessionCapabilities": session_caps,
                        "fs": {"readTextFile": False, "writeTextFile": False},
                        "terminal": False,
                    },
                },
            )
        elif method == "session/new":
            _reply(frame_id, {"sessionId": self.session_id})
            # 让测试能从外部看出走的是哪条路
            sys.stderr.write("FAKE:session/new\n")
            sys.stderr.flush()
        elif method == "session/load":
            # ★ 与真 adapter 一样按 schema 校验：sessionId / cwd / mcpServers
            #   都是必填。不校验的话，调用方漏传 mcpServers 也照样通过 ——
            #   而真环境里那会让恢复静默失败、每轮从头开始。
            params = frame.get("params") or {}
            missing = [k for k in ("sessionId", "cwd", "mcpServers") if k not in params]
            if missing:
                _write(
                    {
                        "jsonrpc": "2.0",
                        "id": frame_id,
                        "error": {
                            "code": -32602,
                            "message": "Invalid params",
                            "data": {"missing": missing},
                        },
                    }
                )
                return
            # ★ 先重放历史，再回响应。ACP 规定 session/load 必须把整段会话
            #   以 session/update 重放一遍 —— 真 adapter 就是这么做的。
            #
            #   这里早先只回一个空 result，于是「重放的内容会不会混进本轮」
            #   这个问题在测试里**根本不存在**。而真 CLI 上它稳定复现：重放
            #   的正文被当作本轮输出累加，落库的助手消息成了历史与本轮的
            #   拼接（用户问一句话，答案里先把前两轮原样又说一遍）。
            _update(
                self.session_id,
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": REPLAYED_TEXT},
                },
            )
            _update(
                self.session_id,
                {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"type": "text", "text": "上一轮的思考"},
                },
            )
            _reply(frame_id, {})
            sys.stderr.write("FAKE:session/load\n")
            sys.stderr.flush()
        elif method == "session/prompt":
            await self._prompt(frame_id, frame.get("params") or {})
        elif method == "session/cancel":
            pass  # notification，不回
        else:
            _write(
                {
                    "jsonrpc": "2.0",
                    "id": frame_id,
                    "error": {"code": -32601, "message": f"未知方法 {method}"},
                }
            )

    async def _prompt(self, frame_id: Any, params: dict[str, Any] | None = None) -> None:
        if self.write_file:
            # 相对路径 —— 落在 cwd，也就是 bridge 传给我们的工作区挂载点。
            text = json.dumps(params or {}, ensure_ascii=False)
            pathlib.Path(self.write_file).write_text(text, encoding="utf-8")

        _update(self.session_id, {"sessionUpdate": "agent_thought_chunk",
                                  "content": {"type": "text", "text": "先看代码"}})
        _update(self.session_id, {"sessionUpdate": "agent_message_chunk",
                                  "content": {"type": "text", "text": "好的，"}})

        if self.ask_permission:
            decision = await self._ask_permission()
            _update(
                self.session_id,
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": f"[决定={decision}]"},
                },
            )

        if self.prompt_delay_s:
            await asyncio.sleep(self.prompt_delay_s)

        _update(self.session_id, {"sessionUpdate": "agent_message_chunk",
                                  "content": {"type": "text", "text": "做完了。"}})
        _reply(
            frame_id,
            {"stopReason": "end_turn", "usage": {"inputTokens": 10, "outputTokens": 5,
                                                 "totalTokens": 15}},
        )

    async def _ask_permission(self) -> str:
        """反向请求：等 bridge（经 server）把用户的决定传回来。"""
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        _write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "session/request_permission",
                "params": {
                    "sessionId": self.session_id,
                    "toolCall": {"toolCallId": "tc-1", "title": "写文件 a.txt"},
                    "options": [
                        {"optionId": "allow", "name": "允许一次", "kind": "allow_once"},
                        {"optionId": "reject", "name": "拒绝", "kind": "reject_once"},
                    ],
                },
            }
        )
        result = await future
        outcome = result.get("outcome") if isinstance(result, dict) else {}
        return str(outcome.get("optionId", "?")) if isinstance(outcome, dict) else "?"


if __name__ == "__main__":
    asyncio.run(FakeAdapter().run())
