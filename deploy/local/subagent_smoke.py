"""native 父智能体委派给 acp 子智能体 —— 全链路冒烟。

    POST /runs（native 父，跑在 server 进程内的图里）
      → 模型调 task 工具 → 子会话 + 子 run
      → AcpRuntime 对子会话原样生效：cluster 建**第二个** Pod
      → 子 Pod 里真 CLI 往 /workspace 写文件
      → 父只拿回结论，用自己的 filesystem 工具读同一份工作区

验收三条：
  ① 委派边界可见：subagent.started / finished 出现在父的事件流里
  ② 子智能体有自己的 Pod 与自己的会话（两个 thread、两个 Pod）
  ③ **工作区共享、技能隔离** —— 子写的文件落在**父会话**的 workspace 前缀下，
     而两边的 skills 前缀各是各的

用法：python deploy/local/subagent_smoke.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
SLUG = "native-parent"
SUB = "cli-hand"
ADAPTER = "node /opt/acp-cli/lib/node_modules/@zed-industries/claude-code-acp/dist/index.js"
IMAGE = "atlas-acp-bridge:0.1.0"


def call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code} {method} {path}\n{exc.read().decode()}", file=sys.stderr)
        raise


def sql(query: str) -> list[list[str]]:
    """直连 compose 里的 Postgres。

    ★ 子 run 的 id 从 API 拿不到：subagent.started 带的 subagent_run_id 是
      **task 的 tool_call_id**（它才是能把 started/finished 配对的键），
      不是 Run.id；而 REST 没有「列出某个 run 的子 run」这个端点。
      本脚本要给子 run 的审批点头，只能直接查库。
    """
    out = subprocess.run(
        ["docker", "exec", "atlas-pg", "psql", "-U", "atlas", "-d", "atlas", "-tA", "-F", "|",
         "-v", "ON_ERROR_STOP=1", "-c", query],
        capture_output=True, text=True, check=False,
    )
    # ★ 必须把 psql 的错误抛出来。早先这里 check=False 又不看 stderr，写错列名
    #   （approval 是 status 不是 decision）只表现为「查不到待审批」—— 于是
    #   子智能体的 Write 一直没人点头，一路卡到 bridge 的 300s 超时，
    #   看着像产品的 bug，实际是本脚本自己瞎了。
    if out.returncode != 0:
        raise RuntimeError(f"psql 失败：{out.stderr.strip()}\n  查询：{query}")
    return [line.split("|") for line in out.stdout.strip().splitlines() if line.strip()]


def ensure_agent() -> str:
    for agent in call("GET", "/v1/agents").get("data", []):
        if agent["slug"] == SLUG:
            return agent["id"]
    created = call(
        "POST",
        "/v1/agents",
        {
            "slug": SLUG,
            "name": "native 父（带 acp 子）",
            "spec": {
                # kind 不写 = native。★ 只有 native 能委派：acp 的工具面由 CLI
                #   自带，平台的 task 工具进不到它的图里。
                "system_prompt": (
                    "你是协调者。需要在工作区动手写文件的活，一律委派给子智能体 "
                    f"{SUB}，不要自己写。委派完成后用 filesystem 工具读回文件确认。"
                ),
                "model": {"model": "deepseek-chat", "provider": "anthropic"},
                "tool_names": ["task", "filesystem"],
                "limits": {"timeout_s": 900},
                "subagents": [
                    {
                        "name": SUB,
                        "description": "在真 CLI 里动手改文件的执行者",
                        "system_prompt": "你在会话 Pod 里，工作区是当前目录。按要求写文件。",
                        "model": {"model": "deepseek-chat", "provider": "anthropic"},
                        # ★ 这一行就是本次验证的主角
                        "kind": "acp",
                        "cli": {"cli_type": "claude-code", "adapter": ADAPTER, "image": IMAGE},
                    }
                ],
            },
        },
    )
    return created["id"]


def auto_approve(thread_id: str, stop: threading.Event) -> None:
    """给本会话**及其子会话**的所有待审批点头（命令行里没有弹窗）。"""
    seen: set[str] = set()
    while not stop.wait(1.0):
        rows = sql(
            "SELECT a.id, r.id, a.tool_name FROM approval a JOIN run r ON r.id = a.run_id "
            "JOIN thread t ON t.id = r.thread_id "
            f"WHERE a.status = 'pending' AND (t.id = '{thread_id}' "
            f"OR t.parent_thread_id = '{thread_id}')"
        )
        for approval_id, run_id, tool_name in rows:
            if approval_id in seen:
                continue
            seen.add(approval_id)
            print(f"    >> 放行审批：{tool_name}（run {run_id[:8]}）")
            try:
                call("POST", f"/v1/runs/{run_id}/approvals/{approval_id}",
                     {"decision": "approved"})
            except Exception as exc:  # noqa: BLE001
                print(f"    !! 放行失败：{exc}")


def stream(run_id: str, timeout_s: float = 900.0) -> None:
    req = urllib.request.Request(
        f"{BASE}/v1/runs/{run_id}/events", headers={"Accept": "text/event-stream"}
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    counts: dict[str, int] = {}
    text: list[str] = []
    with opener.open(req, timeout=timeout_s) as resp:
        for raw in resp:
            line = raw.decode().rstrip("\n")
            if not line.startswith("data:"):
                continue
            e = json.loads(line[5:].strip())
            etype, data = e.get("type", "?"), (e.get("data") or {})
            counts[etype] = counts.get(etype, 0) + 1
            if etype == "message.delta":
                text.append(str(data.get("text", "")))
                continue
            if etype == "thinking.delta":
                continue
            brief = {k: v for k, v in data.items()
                     if k in ("name", "task", "status", "subagent_run_id", "reason")}
            print(f"  [{e.get('seq')}] {etype} {brief if brief else ''}")
            if etype in ("run.finished", "run.succeeded", "run.failed", "run.cancelled"):
                break
    print("\n父智能体的回答：", "".join(text).strip()[:300])
    print("事件计数：", json.dumps(counts, ensure_ascii=False))


def main() -> None:
    agent_id = ensure_agent()
    thread_id = call("POST", "/v1/threads", {"agent_id": agent_id, "title": "委派冒烟"})["id"]
    print(f"父 agent   = {agent_id}\n父 thread  = {thread_id}\n")

    stop = threading.Event()
    threading.Thread(target=auto_approve, args=(thread_id, stop), daemon=True).start()

    t0 = time.time()
    run = call(
        "POST",
        f"/v1/threads/{thread_id}/runs",
        {"content": [{"type": "text", "text":
                      "请委派子智能体在工作区新建 report.md，内容只写一行："
                      "来自子智能体。完成后你自己读回这个文件，把内容告诉我。"}]},
    )
    print("父的事件流：")
    try:
        stream(run["run_id"])
    finally:
        stop.set()
    print(f"\n耗时 {time.time() - t0:.1f}s")

    # ── 结构核对 ────────────────────────────────────────────────────
    print("\n=== 子会话 ===")
    for tid, title, sub_name, cli_session in sql(
        "SELECT t.id, t.title, coalesce(t.subagent_name,'-'), "
        "coalesce(t.external_session_id,'(无)') FROM thread t "
        f"WHERE t.parent_thread_id = '{thread_id}'"
    ):
        print(f"  thread={tid}\n    子智能体={sub_name}  标题={title}\n    CLI 会话={cli_session}")

    print("\n=== 两个会话各自的 Pod ===")
    subprocess.run(["kubectl", "-n", "atlas-sessions", "get", "pods", "--no-headers"], check=False)


if __name__ == "__main__":
    main()
