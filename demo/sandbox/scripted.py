"""假模型：按剧本发 tool_call，不需要 API key。

PLANS：用户说的话 → 若干批 tool_call（同一批的并行执行）+ 最终答复。
tool_call id 按会话里已有的调用数接着编号（c1、c2 …），同样的历史一定得到同样的 id。
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from tracing import say

R1 = "统计 notes.txt 里出现最多的词"
R2 = "跑一下测试"
R3 = "我在界面上传了新的 notes.txt，再统计一次"
R4 = "再看一下 top.txt，顺便确认环境"
R5 = "跑一遍回归测试"

NOTES = "the quick brown fox jumps over the lazy dog\nthe fox sleeps\n"
NOTES_V2 = "alpha beta alpha gamma alpha beta\n"
COUNT_PY = """import collections
import sys

words = open(sys.argv[1]).read().split()
for word, n in collections.Counter(words).most_common(2):
    print(word, n)
"""
TESTS = (
    'for i in 1 2 3 4 5; do echo "case $i ok"; echo $i >> ran.log; sleep 0.1; done;'
    ' echo "ran.log 共 $(grep -c . ran.log) 行"'
)
REGRESSION = (
    'for i in 1 2 3 4 5 6 7 8; do echo "regression $i ok"; echo $i >> reg.log; sleep 0.1; done;'
    " echo 全部通过"
)


def sh(command: str, timeout: int | None = None) -> tuple[str, dict[str, Any]]:
    return "execute", {"command": command, **({"timeout": timeout} if timeout else {})}


def fs(name: str, path: str, content: str | None = None) -> tuple[str, dict[str, Any]]:
    return name, {"path": path, **({"content": content} if content is not None else {})}


PLANS: dict[str, list] = {
    R1: [
        [fs("write_file", "notes.txt", NOTES)],  # 此时还没有沙箱：直连对象存储
        [  # 同一批两个 execute：两个 ensure 同时到达，只建一个沙箱
            sh("python3 --version"),
            sh(
                "mkdir -p ~/.cache && echo wordfreq > ~/.cache/installed"
                " && echo 'HOME 里装好了 wordfreq'"
            ),
        ],
        [fs("write_file", "count.py", COUNT_PY)],  # 沙箱在跑了：经 execd 写进同一个挂载
        [
            sh("python3 count.py notes.txt > top.txt && cat top.txt")
        ],  # 刚写的 count.py，命令马上看得到
        [fs("read_file", "top.txt")],  # 命令写的文件，文件工具马上读得到
        "出现最多的是 the（3 次），其次是 fox（2 次）。",
    ],
    R2: [
        [
            sh(
                '(for i in 1 2 3 4 5 6; do echo "step $i" >> progress.log; sleep 0.15; done)'
                ' & echo "后台任务已启动"'
            )
        ],
        [fs("read_file", "progress.log")],  # 后台进程还在写：同一个挂载，读到的是此刻的内容
        [sh(TESTS)],  # 剧本在它写到第 2 行时把连接断一次
        [sh("while true; do echo tick; sleep 0.1; done", timeout=1), sh("seq 1 30000")],
        "5 个用例都过了；死循环到 1 秒被结束；seq 的输出很大，只看了头尾。",
    ],
    R3: [
        [sh("python3 count.py notes.txt")],  # 上传刚发生：挂载的缓存还没过期
        [fs("read_file", "notes.txt")],  # 文件工具与命令看到的一样（同一个挂载）
        [sh("sleep 1 && python3 count.py notes.txt")],  # 结果没变，等一下再跑
        "按新上传的数据：alpha 出现 3 次，beta 2 次。",
    ],
    R4: [
        [fs("read_file", "top.txt")],  # 沙箱已被回收：路由表没有记录，直连对象存储
        [
            sh(
                "cat ~/.cache/installed 2>/dev/null || echo 'HOME 里装过的东西没了';"
                " python3 count.py notes.txt"
            )
        ],
        "top.txt 还在；沙箱重建过，之前装的东西需要重装。",
    ],
    R5: [
        [sh(REGRESSION)],
        [sh(REGRESSION)],  # 上一次执行中沙箱没了、结果丢失：模型决定重跑
        "回归测试全部通过（第一次执行时沙箱重启，重跑了一遍）。",
    ],
}


def clip(text: str, n: int = 44) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= n else flat[: n - 1] + "…"


def brief(content: object) -> str:
    lines = [line for line in str(content).strip().split("\n") if line.strip()]
    if len(lines) <= 1:
        return clip(lines[0] if lines else "（空）", 70)
    return f"{clip(lines[0], 36)} …（共 {len(lines)} 行）… {clip(lines[-1], 36)}"


def describe(call: dict[str, Any]) -> str:
    args = call["args"]
    if call["name"] == "execute":
        timeout = f"，timeout={args['timeout']}" if "timeout" in args else ""
        return f"{call['id']} execute「{clip(args['command'], 30)}」{timeout}"
    return f"{call['id']} {call['name']} {args['path']}"


class ScriptedModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedModel:
        return self

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        start = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
        turn = messages[start:]
        *batches, answer = PLANS[str(turn[0].content)]
        sent = [m for m in turn if isinstance(m, AIMessage) and m.tool_calls]
        if sent:  # 读上一批的结果
            ids = {c["id"] for c in sent[-1].tool_calls}
            for m in turn:
                if isinstance(m, ToolMessage) and m.tool_call_id in ids:
                    say("模型", f"读到 {m.tool_call_id}：{brief(m.content)}")
        if len(sent) == len(batches):
            say("模型", f"最终答复：{answer}")
            return ChatResult(generations=[ChatGeneration(message=AIMessage(answer))])
        n = sum(len(m.tool_calls) for m in messages if isinstance(m, AIMessage))
        calls = [
            {"name": name, "args": args, "id": f"c{n + i}"}
            for i, (name, args) in enumerate(batches[len(sent)], 1)
        ]
        say("模型", f"同一批发出 {len(calls)} 个调用：{'、'.join(describe(c) for c in calls)}")
        return ChatResult(generations=[ChatGeneration(message=AIMessage("", tool_calls=calls))])
