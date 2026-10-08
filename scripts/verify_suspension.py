"""对**运行中的后端**验证挂起与恢复（doc/detail/suspension.html）。

    make serve            # 另一个终端
    uv run python scripts/verify_suspension.py

为什么需要它：单测用假模型跑真链路，能钉住状态机与事件流，但钉不住
「真模型真的会在这里发 tool call」「decide → 续跑 → 工具真的执行了」这条
端到端的手感。这个脚本走真实模型，**会产生少量计费**。

两条路径各验一遍：

    审批挂起   run 落 awaiting_approval → 决策 → 续跑 → 工具执行 → succeeded
    委派挂起   run 落 suspended        → 子 run 跑完 → 自动续跑 → succeeded

★ 委派那条把 `subagent_inline_wait_s` 压到 0 才能稳定观察到挂起（默认 60s
  的窗口内秒级委派会走同步路径 —— 那正是它存在的意义）。脚本靠环境变量
  覆盖，所以要在启动后端**之前**设好：

    SUBAGENT_INLINE_WAIT_S=0 make serve
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

import httpx

BASE = os.environ.get("API_BASE", "http://127.0.0.1:8000")
MODEL = os.environ.get("VERIFY_MODEL", "deepseek-chat")

failures = 0


def check(ok: bool, label: str, extra: str = "") -> None:
    global failures
    print(f"{'  ✓' if ok else '  ✗'} {label}{f' — {extra}' if extra else ''}")
    if not ok:
        failures += 1


async def api(c: httpx.AsyncClient, method: str, path: str, **kw):
    res = await c.request(method, path, **kw)
    if res.status_code >= 400:
        raise RuntimeError(f"{method} {path} → {res.status_code} {res.text}")
    return res.json() if res.content else None


async def wait_status(
    c: httpx.AsyncClient, run_id: str, wanted: set[str], *, timeout: float = 120.0
) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    last: dict = {}
    while asyncio.get_running_loop().time() < deadline:
        last = await api(c, "GET", f"/v1/runs/{run_id}")
        if last["status"] in wanted:
            return last
        await asyncio.sleep(0.3)
    raise AssertionError(f"run 停在 {last.get('status')!r}，没进入 {wanted}")


async def thread_events(c: httpx.AsyncClient, thread_id: str) -> list[dict]:
    """读一遍会话流已有的部分。会话流永不结束 —— 靠超时掐断。"""
    out: list[dict] = []
    try:
        async with asyncio.timeout(4.0):
            async with c.stream(
                "GET", f"/v1/threads/{thread_id}/events?after_seq=0", timeout=None
            ) as res:
                buf = ""
                async for chunk in res.aiter_text():
                    buf += chunk
                    while "\n\n" in buf:
                        frame, buf = buf.split("\n\n", 1)
                        for line in frame.splitlines():
                            if line.startswith("data: "):
                                out.append(json.loads(line[6:]))
    except (TimeoutError, asyncio.CancelledError):
        pass
    return out


# ──────────────────────────────────────────────── 审批挂起


async def verify_approval(c: httpx.AsyncClient) -> None:
    print("\n场景 1 · 审批挂起（等人点头，可跨重启）")

    agent = await api(
        c,
        "POST",
        "/v1/agents",
        json={
            "slug": f"verify-approval-{uuid.uuid4().hex[:6]}",
            "name": "审批验证",
            "spec": {
                "system_prompt": "你会用 write_todos 记录计划。用户让你记待办时，直接调用它。",
                "model": {"model": MODEL},
                "tool_names": ["write_todos"],
                "limits": {"require_approval_for": ["write_todos"]},
            },
        },
    )
    thread = await api(c, "POST", "/v1/threads", json={"agent_id": agent["id"], "title": ""})
    tid = thread["id"]
    run = await api(
        c,
        "POST",
        f"/v1/threads/{tid}/runs",
        json={"content": [{"type": "text", "text": "把「写周报」和「订会议室」记成两条待办。"}]},
    )
    rid = run["run_id"]

    # ① 落成 awaiting_approval —— 挂起态，**没有进程在等**
    state = await wait_status(c, rid, {"awaiting_approval", "succeeded", "failed"})
    check(
        state["status"] == "awaiting_approval",
        "run 落成 awaiting_approval（挂起，不是阻塞）",
        state["status"],
    )
    if state["status"] != "awaiting_approval":
        print("    ⚠ 模型没调用 write_todos —— 换个提示词或模型再试")
        return

    # ② 待决端点查得到
    pending = (await api(c, "GET", f"/v1/runs/{rid}/approvals"))["data"]
    check(len(pending) == 1, "待决审批可查", f"{len(pending)} 条")
    approval_id = pending[0]["id"]

    # ③ 事件在**会话流**上（前端订阅的就是它）
    events = await thread_events(c, tid)
    kinds = [e["type"] for e in events]
    check("approval.required" in kinds, "approval.required 出现在会话流上")
    check(
        any(e["type"] == "approval.required" and e["thread_seq"] > 0 for e in events),
        "事件带会话级序号",
    )

    # ④ 决策 → 唤醒 → 续跑 → 工具真的执行
    await api(
        c, "POST", f"/v1/runs/{rid}/approvals/{approval_id}", json={"decision": "approved"}
    )
    final = await wait_status(c, rid, {"succeeded", "failed", "cancelled"})
    check(final["status"] == "succeeded", "决策后续跑到终态", final["status"])

    after = await thread_events(c, tid)
    types = [e["type"] for e in after]
    check("tool.completed" in types, "工具在续跑段真的执行了（批准之后才跑）")
    check(
        types.count("run.started") >= 2,
        "这一轮分了多段执行",
        f"run.started ×{types.count('run.started')}",
    )
    check(
        all(e["run_id"] == rid for e in after if e["type"] == "run.started"),
        "多段属于**同一个** run —— 对外仍是一轮",
    )

    seqs = [e["thread_seq"] for e in after]
    check(seqs == sorted(seqs) and len(seqs) == len(set(seqs)), "会话序号严格递增无重复")


# ──────────────────────────────────────────────── 委派挂起


async def verify_delegation(c: httpx.AsyncClient) -> None:
    print("\n场景 2 · 委派挂起（等子智能体，进程重启不丢）")

    inline = os.environ.get("SUBAGENT_INLINE_WAIT_S")
    if inline not in ("0", "0.0"):
        print("    ⊘ 跳过：需要后端以 SUBAGENT_INLINE_WAIT_S=0 启动")
        print("      否则秒级委派走同步路径，观察不到挂起（那正是该配置的意义）")
        return

    agent = await api(
        c,
        "POST",
        "/v1/agents",
        json={
            "slug": f"verify-deleg-{uuid.uuid4().hex[:6]}",
            "name": "委派验证",
            "spec": {
                "system_prompt": "需要写代码时委派给 coder，不要自己写。",
                "model": {"model": MODEL},
                "tool_names": ["task"],
                "subagents": [
                    {
                        "name": "coder",
                        "description": "写代码",
                        "system_prompt": "你是程序员，简短作答。",
                        "model": {"model": MODEL},
                        "session_mode": "persistent",
                    }
                ],
            },
        },
    )
    thread = await api(c, "POST", "/v1/threads", json={"agent_id": agent["id"], "title": ""})
    tid = thread["id"]
    run = await api(
        c,
        "POST",
        f"/v1/threads/{tid}/runs",
        json={"content": [{"type": "text", "text": "让 coder 写一个 Python 的冒泡排序。"}]},
    )
    rid = run["run_id"]

    final = await wait_status(c, rid, {"succeeded", "failed", "cancelled"}, timeout=180)
    check(final["status"] == "succeeded", "委派挂起后自动续跑到终态", final["status"])

    events = await thread_events(c, tid)
    types = [e["type"] for e in events]
    check("run.suspended" in types, "经过了 run.suspended")
    check(
        "run.suspended" in types and types.index("run.suspended") < types.index("run.finished"),
        "挂起不是终态 —— 后面还跟着真正的结束",
    )
    check(any(e["depth"] == 1 for e in events), "子智能体的事件带 depth=1（在同一条流上）")
    check(
        any(e["type"] == "subagent.started" for e in events), "委派边界事件可见"
    )


async def main() -> None:
    # ★ trust_env=False：macOS 的系统代理设置会被 httpx 读走（本机实测
    #   http://127.0.0.1:58591），于是发往 127.0.0.1:8000 的请求也被丢给代理，
    #   表现是每个请求都 ReadTimeout。本地服务一律直连。
    async with httpx.AsyncClient(base_url=BASE, timeout=30.0, trust_env=False) as c:
        health = await api(c, "GET", "/healthz")
        print(f"后端 {BASE} → {health}")
        await verify_approval(c)
        await verify_delegation(c)

    print(f"\n{'全部通过' if not failures else f'{failures} 项未通过'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
