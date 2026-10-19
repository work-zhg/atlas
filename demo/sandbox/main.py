"""沙箱：原生智能体在会话级 Pod 里执行命令。

按需建 Pod、经 sandboxd 执行、文件一致性屏障、断联恢复。

运行：  .venv/bin/python demo/sandbox/main.py   （不需要 API key，约 8 秒）
说明：  demo/sandbox/README.html（原理、文件结构、时序图、动手实验）

★ 真实的部分：命令真的由 bash 执行（setsid 进程组、stdin=/dev/null、杀整组）；
  sandboxd 是 127.0.0.1 上真实的 HTTP 服务，server 用 httpx 读它的 NDJSON 流。
  内存模拟的部分：K8s、cluster、对象存储、rclone 挂载器（挂载点是真实的本地目录）。

场景：用户 u1 在会话 t1 里，和一个勾了 Shell 的原生智能体说了六句话。

    r1  首次执行    同一批两个 execute → 两个 ensure 串行，只建一个 Pod；文件工具写进对象存储 →
                    执行前 forget，命令看得到；执行后等写回，read_file 读得到命令的产物
    r2  长命令      事件流断了一下 → POST 同一个 exec_id 带 after 接上，不重跑；安静的计算不算卡死，
                    sleep 30 算；一直有输出的死循环靠模型给的 timeout；大输出只留头尾
    r3  后台与取消  命令以 shell 退出为准，后台进程不挡结果；编译时闲置回收不回收；
                    用户取消 → 杀整个进程组
    r4  闲置回收    Pod 闲置被删 → 下一次 execute 重建 → 结果前提示「环境已重建」：
                    工作区还在，HOME 没了
    r5  Pod 消失    执行中 Pod 被删 → 重连拿到 409 → 工具错误回给模型（不重跑、整轮不失败）
                    → 模型重跑
    r6  server 崩溃 实例 A 执行中崩溃 → 实例 B 接管、重放同一个 tool_call → 同一个 exec_id
                    → 接上原命令

文件按功能拆分（依赖方向从上到下）：
    main.py            剧本：六轮对话，中途按剧本注入故障，最后打印各处状态
    executor.py        server 进程：跑一轮（建图、装 PodSandbox、落库）；模拟进程崩溃
    agent.py           kernel：SandboxProtocol 契约、execute 工具、文件工具、组装图
    scripted.py        假模型（剧本）
    sandbox_client.py  server：PodSandbox —— 按需 ensure、NDJSON 流、断线接上、boot_id、取消
    cluster.py         cluster 服务（ensure / release / 闲置回收）+ K8s（内存模拟）
    pod.py             一个沙箱 Pod：挂载器 + barrier + sandboxd
    sandboxd.py        Pod 里的守护进程：HTTP 接口 + 真实地执行命令
    storage.py         对象存储、rclone 挂载器（缓存与写回队列）、barrier sidecar
    tracing.py         输出：每行带上是哪一层在说话
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import Callable

from cluster import IDLE_TTL_S, FakeK8s, PodManager, pod_name
from executor import Conversation, InstanceCrashed, ServerInstance
from langchain_core.messages import HumanMessage
from sandboxd import Exec
from scripted import R1, R2, R3, R4, R5, R6
from storage import ObjectStorage
from tracing import say

THREAD, USER = "t1", "u1"


def scene(title: str) -> None:
    print(f"\n== {title} ==")


async def until(check: Callable[[], object], what: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            raise RuntimeError(f"等了 {timeout:g} 秒，{what} 还没发生")
        await asyncio.sleep(0.01)


def find_exec(k8s: FakeK8s, exec_id: str) -> Exec | None:
    """按 exec_id 找；找不到就按前缀找（实验里 tool_call id 可能被改成带后缀的）。"""
    pod = k8s.pods.get(pod_name(THREAD))
    if not pod or not pod.manager:
        return None
    return pod.manager.get(exec_id) or next(
        (e for e in pod.manager.list() if e.id.startswith(exec_id)), None
    )


def outputs(k8s: FakeK8s, exec_id: str) -> int:
    ex = find_exec(k8s, exec_id)
    return sum(e["t"] == "output" for e in ex.events) if ex else 0


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)  # 信号 0：只检查进程组里还有没有进程
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def user_says(db: Conversation, run_id: str, text: str) -> None:
    print(f"\n用户（{run_id}）：「{text}」")
    db.messages.append(HumanMessage(text))


async def main() -> None:
    storage = ObjectStorage()
    k8s = FakeK8s(storage)
    cluster = PodManager(k8s)
    db = Conversation(THREAD, USER)
    a = ServerInstance("A", cluster, storage, db)
    b = ServerInstance("B", cluster, storage, db)
    started = time.monotonic()

    scene("r1 首次执行：按需建 Pod、两个 ensure 串行、文件一致性屏障")
    user_says(db, "r1", R1)
    await asyncio.create_task(a.run_turn("r1"))

    scene("r2 长命令：断线接上、卡死判定、显式超时、输出截断")
    k8s.faults.cut_after["r2:c7"] = 3
    say("剧本", "r2:c7 的事件流发到 seq 3 时让网络断一下")
    user_says(db, "r2", R2)
    await asyncio.create_task(a.run_turn("r2"))

    scene("r3 后台进程与取消：命令以 shell 退出为准；有命令在跑不回收；取消就杀整个进程组")
    user_says(db, "r3", R3)
    task = asyncio.create_task(a.run_turn("r3"))
    await until(lambda: (ex := find_exec(k8s, "r3:c13")) and ex.state == "running", "r3:c13 开始")
    await asyncio.sleep(0.2)
    await cluster.reap_idle()
    compile_exec = find_exec(k8s, "r3:c13")
    assert compile_exec is not None and compile_exec.pgid is not None
    pgid = compile_exec.pgid
    say("用户", "点了「停止」→ 取消 r3")
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await until(lambda: not group_alive(pgid), f"进程组 {pgid} 清空", timeout=2)
    say("剧本", f"r3:c13 的进程组 {pgid} 已清空：shell 和它起的 sh -c 'sleep 30' 都没了")
    watcher = find_exec(k8s, "r3:c12")
    assert watcher is not None
    await until(lambda: "后台进程" in watcher.log_path.read_text(), "c12 的后台进程打完它那一行")
    log = " / ".join(watcher.log_path.read_text().split("\n")).strip(" /")
    say("剧本", f"r3:c12 的完整日志（{watcher.log_path.name}）：{log}")

    scene("r4 闲置回收：Pod 被删，下一次 execute 重建；工作区还在，HOME 没了")
    await asyncio.sleep(IDLE_TTL_S)
    await cluster.reap_idle()
    user_says(db, "r4", R4)
    await asyncio.create_task(a.run_turn("r4"))

    scene("r5 Pod 在命令执行中消失：重连拿到 409，不重跑；工具错误回给模型")
    user_says(db, "r5", R5)
    task = asyncio.create_task(a.run_turn("r5"))
    await until(lambda: outputs(k8s, "r5:c15") >= 3, "r5:c15 输出 3 行")
    say("剧本", f"节点故障：{pod_name(THREAD)} 被删（r5:c15 执行到一半）")
    await k8s.delete_pod(pod_name(THREAD))
    await task

    scene("r6 server 崩溃：实例 B 接管，重放同一个 tool_call → 同一个 exec_id → 接上原命令")
    user_says(db, "r6", R6)
    task = asyncio.create_task(a.run_turn("r6"))
    await until(lambda: outputs(k8s, "r6:c17") >= 2, "r6:c17 输出 2 行")
    say("剧本", "实例 A 崩溃（进程被 OOM 杀掉）")
    a.crash()
    with contextlib.suppress(InstanceCrashed):
        await task
    db.runs["r6"] = "running（实例 A 已死，等接管）"
    await asyncio.sleep(0.25)
    say("剧本", "实例 B 接管 r6：从库里读历史，重放这一轮（前提见 README §7.6）")
    await asyncio.create_task(b.run_turn("r6"))

    print(f"\n整轮用时约 {time.monotonic() - started:.1f} 秒")
    scene("最终状态")
    print("  run 表")
    for run_id, status in db.runs.items():
        print(f"    {run_id}  {status}")
    prefix = f"{USER}/{THREAD}/workspace/"
    files = [
        f"{name}（{data.decode().count(chr(10))} 行）" if name.endswith(".log") else name
        for name, data in sorted(storage.under(prefix).items())
    ]
    print(f"  对象存储 {prefix}：{'、'.join(files)}")
    pod = k8s.pods[pod_name(THREAD)]
    secret_names = "、".join(k8s.secrets)
    print(f"  K8s：Pod {pod.name}（boot {pod.boot_id}），Secret {secret_names}")
    print(f"  先后一共建过 {k8s._n} 个 Pod：r1 首次执行、r4 闲置回收之后、r5 执行中被删之后")
    say("剧本", "会话删除 → cluster.release：删 Pod 和 Secret")
    await cluster.release(THREAD)
    print(f"  K8s：Pod {len(k8s.pods)} 个，Secret {len(k8s.secrets)} 个")
    k8s.close()


if __name__ == "__main__":
    asyncio.run(main())
