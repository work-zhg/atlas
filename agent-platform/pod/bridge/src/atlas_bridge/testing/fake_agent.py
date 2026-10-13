"""FakeAgent：按剧本行事的 ACP agent（代码设计 §12.2）。

两种用法：
  · 进程内：``FakeAgent(script).serve(channel)``，channel 通常是 memory_pair 的一端
  · 独立进程：``python -m atlas_bridge.testing.fake_agent '<剧本 JSON>'``，走真实的 stdio
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from atlas_jsonrpc import ErrorCode, MessageChannel, RpcEndpoint, RpcFault

__all__ = ["FakeAgent", "Script"]

DEFAULT_CAPS: dict[str, Any] = {
    "loadSession": True,
    "sessionCapabilities": {"resume": {}, "close": {}},
    "mcpCapabilities": {"http": True, "sse": False},
    "promptCapabilities": {"embeddedContext": True},
}


@dataclass
class Script:
    """一个 FakeAgent 的行为。全部字段都有默认值：默认就是「正常完成」。"""

    protocol_version: int = 1
    capabilities: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_CAPS))
    #: 每一轮先发几条正文 update
    updates_per_turn: int = 2
    #: 这一轮要不要请求一次权限（在正文之后）
    ask_permission: bool = False
    #: 收到 prompt 后：complete = 正常完成；silent = 永不响应（卡死）；crash = 进程退出；
    #: error = 回 JSON-RPC 错误；no_stop_reason = 响应里缺 stopReason（违反规范）
    on_prompt: Literal["complete", "silent", "crash", "error", "no_stop_reason"] = "complete"
    #: 收到 session/new · load · resume 后：ok = 正常；silent = 永不响应；crash = 进程退出；
    #: error = 回 JSON-RPC 错误（恢复与新建都失败）
    on_open: Literal["ok", "silent", "crash", "error"] = "ok"
    #: 收到 cancel 后：confirm = 以 cancelled 结束；ignore = 不理会；
    #: finish = 恰好做完，以 end_turn 结束；error = 回错误（违反规范）
    on_cancel: Literal["confirm", "ignore", "finish", "error"] = "confirm"
    #: >0 时，在权限请求之前先输出一行这么多字节的 update（测试超长行）
    huge_line_bytes: int = 0
    #: 建会话后、没有任何一轮在进行时，主动发几条 update（测试 stray）
    background_updates: int = 0
    #: 启动时往 stderr 写的内容
    stderr_lines: list[str] = field(default_factory=list)
    #: 启动时拉起一个长时间运行的子进程，并把它的 pid 写到 stderr（测试进程组终止）
    spawn_child: bool = False
    #: 支持的权限模式（ACP session modes）。空 = 不支持模式：会话响应里不带 modes
    modes: list[str] = field(default_factory=lambda: ["default", "acceptEdits", "plan", "auto"])
    #: 新建 / 恢复会话时的模式 —— 与真实 adapter 一样，每次打开会话都会回到它
    initial_mode: str = "default"
    #: 设置某个模式时自行降级成另一个（模拟模型不支持 auto 时 auto → acceptEdits）
    mode_fallback: dict[str, str] = field(default_factory=dict)
    #: session/set_mode 回错误
    set_mode_fails: bool = False

    def to_json(self) -> str:
        return json.dumps(asdict(self))


class FakeAgent:
    def __init__(self, script: Script | None = None, *, exit_on_crash: bool = False) -> None:
        self.script = script or Script()
        #: 独立进程模式下「崩溃」就是真的退出进程；进程内模式下是关闭通道
        self._exit_on_crash = exit_on_crash
        self._sessions = itertools.count(1)
        self._endpoint: RpcEndpoint | None = None
        self._channel: MessageChannel | None = None
        self._cancel = asyncio.Event()
        #: 记录收到的请求（测试用）：(method, params)
        self.received: list[tuple[str, Any]] = []
        #: 每个会话的历史：session_id → [prompt 文本]
        self.history: dict[str, list[str]] = {}
        #: 每个会话当前的权限模式
        self.session_mode: dict[str, str] = {}

    async def serve(self, channel: MessageChannel) -> None:
        self._channel = channel
        self._endpoint = RpcEndpoint(
            channel, name="fake-agent", on_request=self._on_request, on_notification=self._on_note
        )
        await self._endpoint.run()

    # ------------------------------------------------------------------ 分派

    async def _on_note(self, method: str, params: Any) -> None:
        self.received.append((method, params))
        if method == "session/cancel":
            self._cancel.set()

    async def _on_request(self, method: str, params: Any) -> Any:
        self.received.append((method, params))
        handler = {
            "initialize": self._initialize,
            "session/new": self._new,
            "session/load": self._load,
            "session/resume": self._resume,
            "session/prompt": self._prompt,
            "session/close": self._close,
            "session/set_mode": self._set_mode,
            "_test/env": self._env,
        }.get(method)
        if handler is None:
            raise RpcFault(ErrorCode.METHOD_NOT_FOUND, f"fake agent 不支持 {method}")
        return await handler(params)

    # ------------------------------------------------------------------ 方法

    async def _initialize(self, params: Any) -> Any:
        return {
            "protocolVersion": self.script.protocol_version,
            "agentCapabilities": self.script.capabilities,
            "agentInfo": {"name": "fake-agent", "version": "0.0.0"},
            "authMethods": [],
        }

    async def _opening(self) -> None:
        """session/new · load · resume 的共同前奏：按剧本卡死、崩溃或报错。"""
        match self.script.on_open:
            case "silent":
                await asyncio.Event().wait()
            case "crash":
                await self._crash()
            case "error":
                raise RpcFault(ErrorCode.INTERNAL_ERROR, "fake agent 打不开会话")

    async def _new(self, params: Any) -> Any:
        await self._opening()
        # 带 pid：独立进程模式下，重启后的新进程不会与旧进程生成相同的 id
        session_id = f"fake-{os.getpid()}-{next(self._sessions)}"
        self.history[session_id] = []
        asyncio.create_task(self._background(session_id))  # noqa: RUF006
        return {"sessionId": session_id, **self._modes_of(session_id)}

    async def _load(self, params: Any) -> Any:
        await self._opening()
        session_id = params["sessionId"]
        if session_id not in self.history:
            raise RpcFault(-32002, "会话不存在")
        for text in self.history[session_id]:  # 按规范：先重放全部历史，再响应
            await self._update(session_id, "user_message_chunk", text)
            await self._update(session_id, "agent_message_chunk", f"答复：{text}")
        return self._modes_of(session_id)

    async def _resume(self, params: Any) -> Any:
        await self._opening()
        if params["sessionId"] not in self.history:
            raise RpcFault(-32002, "会话不存在")
        return self._modes_of(params["sessionId"])

    def _modes_of(self, session_id: str) -> dict[str, Any]:
        """打开会话时报告模式，并把它重置为 initial_mode（真实 adapter 就是这样）。"""
        if not self.script.modes:
            return {}
        self.session_mode[session_id] = self.script.initial_mode
        return {
            "modes": {
                "currentModeId": self.script.initial_mode,
                "availableModes": [{"id": m, "name": m} for m in self.script.modes],
            }
        }

    async def _set_mode(self, params: Any) -> Any:
        session_id, mode = params["sessionId"], params["modeId"]
        if self.script.set_mode_fails or mode not in self.script.modes:
            raise RpcFault(ErrorCode.INVALID_PARAMS, f"Mode {mode} is not available")
        effective = self.script.mode_fallback.get(mode, mode)
        self.session_mode[session_id] = effective
        if effective != mode:  # 自行降级：先发 update 再响应（与真实 adapter 的顺序一致）
            await self._notify(
                session_id, {"sessionUpdate": "current_mode_update", "currentModeId": effective}
            )
        return {}

    async def _close(self, params: Any) -> Any:
        self._cancel.set()
        return {}

    async def _env(self, params: Any) -> Any:
        return {"keys": sorted(os.environ)}

    async def _prompt(self, params: Any) -> Any:
        # cancel 标记在一轮结束时才清：session/cancel 可能先于 prompt 处理任务开始执行就到达
        try:
            return await self._run_prompt(params)
        finally:
            self._cancel.clear()

    async def _run_prompt(self, params: Any) -> Any:
        session_id = params["sessionId"]
        text = "".join(b.get("text", "") for b in params.get("prompt", []) if isinstance(b, dict))
        self.history.setdefault(session_id, []).append(text)
        script = self.script

        if script.on_prompt == "crash":
            await self._crash()
        if script.on_prompt == "error":
            raise RpcFault(ErrorCode.INTERNAL_ERROR, "fake agent 出错了")
        for i in range(script.updates_per_turn):
            await self._update(session_id, "agent_message_chunk", f"第 {i + 1} 段")
        if script.huge_line_bytes:
            await self._huge_line(session_id, script.huge_line_bytes)
        if script.ask_permission:
            granted = await self._ask(session_id)
            if granted is None:  # 权限请求被答复 cancelled：这一轮被取消了
                return await self._settle_cancel()
        if script.on_prompt == "silent":
            await self._cancel.wait()
            return await self._settle_cancel()
        if self._cancel.is_set():
            return await self._settle_cancel()
        if script.on_prompt == "no_stop_reason":
            return {}
        return {"stopReason": "end_turn"}

    async def _settle_cancel(self) -> Any:
        match self.script.on_cancel:
            case "confirm":
                return {"stopReason": "cancelled"}
            case "finish":
                return {"stopReason": "end_turn"}
            case "error":
                raise RpcFault(ErrorCode.INTERNAL_ERROR, "aborted")
            case "ignore":
                await asyncio.Event().wait()  # 永不响应
        raise AssertionError("unreachable")

    async def _ask(self, session_id: str) -> bool | None:
        """请求一次权限。返回 True = 允许，False = 拒绝，None = cancelled。"""
        assert self._endpoint is not None
        tool = {"toolCallId": "call-1", "title": "写入 /workspace/a.txt", "kind": "edit"}
        await self._notify(session_id, {"sessionUpdate": "tool_call", **tool, "status": "pending"})
        result = await self._endpoint.request(
            "session/request_permission",
            {
                "sessionId": session_id,
                # 与真实适配器一致：toolCall 里带着 title（claude-code-acp 实测如此）
                "toolCall": {"toolCallId": "call-1", "title": tool["title"], "kind": "edit"},
                "options": [
                    {"optionId": "allow", "name": "允许", "kind": "allow_once"},
                    {"optionId": "reject", "name": "拒绝", "kind": "reject_once"},
                ],
            },
            timeout=None,
        )
        outcome = result.get("outcome", {})
        if outcome.get("outcome") == "cancelled":
            return None
        granted = outcome.get("optionId") == "allow"
        status = "completed" if granted else "failed"
        await self._notify(
            session_id,
            {"sessionUpdate": "tool_call_update", "toolCallId": "call-1", "status": status},
        )
        return granted

    # ------------------------------------------------------------------ 输出

    async def _update(self, session_id: str, kind: str, text: str) -> None:
        await self._notify(
            session_id, {"sessionUpdate": kind, "content": {"type": "text", "text": text}}
        )

    async def _notify(self, session_id: str, update: dict[str, Any]) -> None:
        assert self._endpoint is not None
        await self._endpoint.notify("session/update", {"sessionId": session_id, "update": update})

    async def _huge_line(self, session_id: str, size: int) -> None:
        assert self._channel is not None
        filler = "x" * size
        frame = {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": filler},
                },
            },
        }
        await self._channel.send(json.dumps(frame))

    async def _background(self, session_id: str) -> None:
        for i in range(self.script.background_updates):
            await asyncio.sleep(0.01)
            await self._update(session_id, "agent_message_chunk", f"后台 {i + 1}")

    async def _crash(self) -> None:
        if self._exit_on_crash:
            os._exit(3)
        assert self._channel is not None
        close = getattr(self._channel, "close", None)
        if close is not None:
            close()  # 进程内：关闭通道，等同于进程退出
        await asyncio.Event().wait()


async def _main(script: Script) -> None:
    from .stdio import StdioChannel

    for line in script.stderr_lines:
        print(line, file=sys.stderr, flush=True)
    if script.spawn_child:
        child = subprocess.Popen(["sleep", "300"])
        print(f"child-pid={child.pid}", file=sys.stderr, flush=True)
    channel = await StdioChannel.open()
    await FakeAgent(script, exit_on_crash=True).serve(channel)


if __name__ == "__main__":
    raw = sys.argv[1] if len(sys.argv) > 1 else "{}"
    asyncio.run(_main(Script(**json.loads(raw))))
