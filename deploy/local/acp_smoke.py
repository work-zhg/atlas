"""acp 全链路冒烟：建 agent → 建会话 → 跑一轮 → 读事件流。

    POST /v1/agents (kind=acp) → POST /v1/threads → POST /threads/{id}/runs
      → server 向 cluster ensure Pod → Pod 起来（rclone 挂 MinIO + 真 CLI）
      → server 连 ws://<podIP>:8900 → session/prompt → update 流
      → TraceEvent → GET /v1/runs/{id}/events (SSE)

用法：
    python deploy/local/acp_smoke.py "帮我在工作区写一个 hello.md"
    python deploy/local/acp_smoke.py --thread <id> "刚才那个文件叫什么？"   # 第二轮 resume
"""

from __future__ import annotations

import argparse
import json
import threading
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
SLUG = "acp-smoke"
# ★ 必须显式给：模板把 cli.adapter 原样写进 ATLAS_ADAPTER_CMD，空字符串会
#   覆盖掉镜像里的默认值，bridge 随即以 exit 2 退出（"缺少必需的环境变量"）。
ADAPTER = "node /opt/acp-cli/lib/node_modules/@zed-industries/claude-code-acp/dist/index.js"
IMAGE = "atlas-acp-bridge:0.1.0"


def call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    # ★ 绕开代理：BASE 是本机地址，走代理会直接失败
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code} {method} {path}\n{exc.read().decode()}", file=sys.stderr)
        raise


def ensure_agent() -> str:
    for agent in call("GET", "/v1/agents").get("data", []):
        if agent["slug"] == SLUG:
            return agent["id"]
    created = call(
        "POST",
        "/v1/agents",
        {
            "slug": SLUG,
            "name": "ACP 冒烟",
            "spec": {
                "kind": "acp",
                "system_prompt": "你是跑在会话 Pod 里的助手。工作区是当前目录。",
                "model": {"model": "deepseek-chat", "provider": "anthropic"},
                "cli": {"cli_type": "claude-code", "adapter": ADAPTER, "image": IMAGE},
                "limits": {"timeout_s": 600},
            },
        },
    )
    return created["id"]


def auto_approve(run_id: str, stop: "threading.Event") -> None:
    """轮询 GET /approvals 并放行 —— 命令行里没有弹窗，用它代替人点头。

    ★ 轮询只是这个脚本图省事。真正的信号是 approval.required 事件，前端的
      弹窗就由它驱动（web/src/lib/events.ts 的 ApprovalRequired）。
      这个事件一度是**不发的** —— 审批落了库没人知道，run 一路卡到 bridge
      的 300s adapter 超时才以 runtime_crashed 失败；修复见
      acp/runtime.py::_decide_permission，回归断言在
      tests/test_acp_end_to_end.py::test_permission_request_becomes_a_platform_approval。
    """
    seen: set[str] = set()
    while not stop.wait(1.0):
        try:
            pending = call("GET", f"/v1/runs/{run_id}/approvals").get("data", [])
        except Exception:
            continue
        for item in pending:
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            print(f"  >> 自动放行审批：{item['tool_name']}")
            call("POST", f"/v1/runs/{run_id}/approvals/{item['id']}", {"decision": "approved"})


def stream_events(run_id: str, timeout_s: float = 600.0) -> None:
    """读 SSE 到 run 终态。打的是**事件类型与关键字段**，不是原始帧 ——
    验收标准是「事件与 native 同形」，形状比内容重要。"""
    req = urllib.request.Request(
        f"{BASE}/v1/runs/{run_id}/events", headers={"Accept": "text/event-stream"}
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.time() + timeout_s
    counts: dict[str, int] = {}
    with opener.open(req, timeout=timeout_s) as resp:
        for raw in resp:
            if time.time() > deadline:
                print("!! 超时", file=sys.stderr)
                return
            line = raw.decode().rstrip("\n")
            if not line.startswith("data:"):
                continue
            payload = json.loads(line[5:].strip())
            etype = payload.get("type", "?")
            counts[etype] = counts.get(etype, 0) + 1
            data = payload.get("data") or {}
            brief = {k: v for k, v in data.items() if k in ("name", "status", "reason", "text")}
            if isinstance(brief.get("text"), str) and len(brief["text"]) > 80:
                brief["text"] = brief["text"][:80] + "…"
            print(f"  [{payload.get('seq')}] {etype} {brief if brief else ''}")
            if etype in ("run.succeeded", "run.failed", "run.cancelled", "run.interrupted"):
                print("\n事件计数：", json.dumps(counts, ensure_ascii=False))
                return


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt")
    ap.add_argument("--thread", default=None, help="复用已有会话 —— 用来验第二轮 resume")
    args = ap.parse_args()

    agent_id = ensure_agent()
    print(f"agent = {agent_id}")

    thread_id = args.thread or call(
        "POST", "/v1/threads", {"agent_id": agent_id, "title": "acp 冒烟"}
    )["id"]
    print(f"thread = {thread_id}")

    t0 = time.time()
    # content 是 Anthropic 的内容块数组，不是裸字符串
    run = call(
        "POST",
        f"/v1/threads/{thread_id}/runs",
        {"content": [{"type": "text", "text": args.prompt}]},
    )
    print(f"run = {run['run_id']}  （POST 返回耗时 {time.time() - t0:.1f}s）\n事件流：")
    stop = threading.Event()
    approver = threading.Thread(target=auto_approve, args=(run["run_id"], stop), daemon=True)
    approver.start()
    try:
        stream_events(run["run_id"])
    finally:
        stop.set()
    print(f"\n总耗时 {time.time() - t0:.1f}s")
    print(f"下一轮复用会话： python deploy/local/acp_smoke.py --thread {thread_id} '…'")


if __name__ == "__main__":
    main()
