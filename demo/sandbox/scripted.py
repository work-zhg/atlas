"""假模型：按剧本发 tool_call，不需要 API key。

PLANS：用户说的话 → 若干批 tool_call（同一批的并行执行）+ 最终答复。
tool_call id 按会话里已有的调用数接着编号（c1、c2 …），所以同样的历史一定得到同样的 id ——
实例 B 接管崩溃的 r6、重放这一轮时，发出的还是 c17（见 main.py 第 6 幕的前提说明）。
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from tracing import say

R1 = "统计 notes.txt 里出现最多的词"
R2 = "跑一下测试"
R3 = "后台起个 watcher，再全量编译一遍"
R4 = "再统计一次"
R5 = "跑一遍回归测试"
R6 = "把测试报告打包"

NOTES = "the quick brown fox jumps over the lazy dog\nthe fox sleeps\n"
COUNT_PY = """import collections
import sys

words = open(sys.argv[1]).read().split()
for word, n in collections.Counter(words).most_common(3):
    print(word, n)
"""
QUIET_CPU = """python3 -c '
import time
end = time.time() + 1
while time.time() < end:
    pass
print("算了 1 秒，期间没有任何输出")'"""
REGRESSION = 'for i in 1 2 3 4 5 6 7 8; do echo "regression $i ok"; sleep 0.1; done; echo 全部通过'


def sh(command: str, timeout: int | None = None) -> tuple[str, dict[str, Any]]:
    return "execute", {"command": command, **({"timeout": timeout} if timeout else {})}


def fs(name: str, path: str, content: str | None = None) -> tuple[str, dict[str, Any]]:
    return name, {"path": path, **({"content": content} if content is not None else {})}


PLANS: dict[str, list] = {
    R1: [
        [  # 同一批两个 execute：此时还没有 Pod，两个 ensure 同时到达 cluster
            sh("python3 --version"),
            sh(
                "mkdir -p ~/.cache/pip && echo wordfreq > ~/.cache/pip/installed"
                " && env | cut -d= -f1 | sort | tr '\\n' ' '"
            ),
        ],
        [fs("write_file", "notes.txt", NOTES), fs("write_file", "count.py", COUNT_PY)],
        [sh("python3 count.py notes.txt > top.txt && cat top.txt")],  # 刚写的文件，命令要看得到
        [fs("read_file", "top.txt")],  # 命令写的文件，文件工具要读得到
        "出现最多的是 the（3 次），其次是 fox（2 次）。",
    ],
    R2: [
        [
            sh(
                'for i in 1 2 3 4 5; do echo "case $i ok"; echo $i >> ran.log; sleep 0.1; done;'
                ' echo "ran.log 共 $(grep -c . ran.log) 行"'
            )
        ],
        [
            sh(QUIET_CPU),  # 安静地算：没有输出，但 CPU 在涨
            sh("sleep 30"),  # 卡住：没有输出，CPU 也不涨
            sh("while true; do echo tick; sleep 0.1; done", timeout=1),  # 死循环但一直有输出
            sh("seq 1 30000"),  # 输出很大
        ],
        "5 个用例都过了。安静的计算正常跑完；sleep 30 被判卡死；"
        "死循环到 1 秒被结束；seq 的输出被截断。",
    ],
    R3: [
        [sh('(sleep 1; echo "watcher：后台进程的输出") & echo "watcher 已在后台启动"')],
        [sh("sh -c 'sleep 30' & sleep 30")],  # 编译，很久
        "编译完成。",
    ],
    R4: [
        [
            sh(
                "python3 count.py notes.txt;"
                ' cat ~/.cache/pip/installed 2>/dev/null || echo "HOME 里装过的依赖没了"'
            )
        ],
        "结果没变：the 3 次、fox 2 次。沙箱重建过，之前装的依赖需要重装。",
    ],
    R5: [
        [sh(REGRESSION)],
        [sh(REGRESSION)],  # 上一次沙箱中途重启、结果丢了：模型决定重跑
        "回归测试全部通过（第一次执行时沙箱重启，重跑了一遍）。",
    ],
    R6: [
        [
            sh(
                'for i in 1 2 3 4 5 6; do echo "packing part $i"; echo $i >> pack.log;'
                ' sleep 0.1; done; echo "pack.log 共 $(grep -c . pack.log) 行"'
            )
        ],
        "打包好了。",
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
        # tool_call id 在整个会话里唯一：接着已有的调用往下编号
        n = sum(len(m.tool_calls) for m in messages if isinstance(m, AIMessage))
        calls = [
            {"name": name, "args": args, "id": f"c{n + i}"}
            for i, (name, args) in enumerate(batches[len(sent)], 1)
        ]
        say("模型", f"同一批发出 {len(calls)} 个调用：{'、'.join(describe(c) for c in calls)}")
        return ChatResult(generations=[ChatGeneration(message=AIMessage("", tool_calls=calls))])
