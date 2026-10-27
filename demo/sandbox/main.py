"""执行环境迁移到 OpenSandbox：原生沙箱与 ACP Pod（doc/sandbox-opensandbox-design.html）。

按需建沙箱、工作区读写路由、后台命令 + 游标轮询、写穿、闲置回收、ACP 的 Credential Vault。

运行：  .venv/bin/python demo/sandbox/main.py   （不需要 API key，约 6 秒）
说明：  demo/sandbox/README.html（原理、文件结构、时序图、动手实验）

★ 真实的部分：命令真的由 bash 执行（独立进程组、stdin=/dev/null、杀整组）；所有组件之间走 HTTP
  （httpx，经一个模拟的「集群网络」分发）；模板合并逻辑照搬 OpenSandbox 上游。
  内存模拟的部分：OpenSandbox 的 server / 控制器 / execd / egress、K8s、对象存储、CSI 挂载（挂载点是
  真实的本地目录）、Redis、bridge 与 CLI、Gitee。

场景：用户 u1 在会话 t1 里和一个勾了 Shell 的原生智能体说了五句话，
      最后 TeamFlow 的会话 t2 跑了一轮 ACP。

    r1  按需建沙箱  文件工具先直连对象存储；同一批两个 execute → 两个 ensure，只建一个沙箱
                    → 写路由表；之后的文件工具走沙箱：刚写的 count.py 命令马上看得到，
                    命令的产物 read_file 马上读得到
    r2  后台与断线  后台进程正在写的文件，read_file 读得到此刻的内容；
                    轮询中连接被重置 → 同一个命令 id、
                    同一个游标接上，不重跑；模型给的 timeout 由 execd 执行；大输出只留头尾
    r3  外部写入    用户在界面上传新的 notes.txt：对象存储立刻是新的，沙箱的挂载要等元数据缓存过期
                    （换实例 B 跑：路由表在 Redis，B 也知道该走沙箱）
    r4  闲置回收    沙箱闲置被回收（先删路由）→ read_file 自动回到对象存储；下一次 execute 重建 →
                    「环境已重建」：工作区还在，HOME 没了
    r5  沙箱消失    执行中 Pod 没了 → 重新 ensure 得到新沙箱 → 它的 execd 不认识这个命令
                    → 工具错误 → 模型重跑
    r6  ACP        opensandbox-acp 建 ACP 的 Pod：模型 key 是模板挂的文件、
                    git 凭据在 Credential Vault；
                    CLI 推送时由 egress 注入认证；Pod 里找不到任何 git 凭据

文件按功能拆分（依赖方向从上到下）：
    main.py               剧本：六幕，中途注入故障，最后打印各处状态
    executor.py           server 进程：跑一轮（建图、装 PodSandbox 与 WorkspaceFilesystem）
    agent.py              kernel：两个契约、execute、文件工具、组装图
    scripted.py           假模型（剧本）
    sandbox_client.py     server：PodSandbox —— 后台命令、游标轮询、断线接上、环境已重建、头尾截断
    workspace.py          server：工作区读写路由 —— 两套底层（对象存储 / execd）
    acp.py                server：OpenSandboxPods、git 档案与 Credential Vault、HostRuntime
    provision.py          server：路由表、ensure（复用 / 锁 / 配额 / 镜像）、闲置回收与孤儿回收
    opensandbox_client.py server：lifecycle 与 execd 的薄客户端
    opensandbox.py        OpenSandbox：两个 lifecycle server、控制器、Pod、egress；集群网络
    execd.py              Pod 里的 execd：HTTP 接口 + 真实执行命令 + 文件 API
    images.py             ACP 镜像里的 bridge 与 CLI；Gitee
    storage.py            对象存储、写穿式 CSI 挂载、Redis
    tracing.py            输出：每行带上是哪一层在说话
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from acp import GIT_PROFILES, AcpRuntime, AcpThread, OpenSandboxPods
from executor import Conversation, ServerInstance
from images import FakeGitee
from langchain_core.messages import HumanMessage
from opensandbox import ACP_TEMPLATE, API_KEYS, SBX_TEMPLATE, Controller, LifecycleServer, Network
from opensandbox_client import LifecycleClient
from provision import ACP, IDLE_TTL_S, SBX, Ensurer, Maintainer, SandboxRoutes, sbx_spec
from scripted import NOTES_V2, R1, R2, R3, R4, R5
from storage import FakeRedis, ObjectStorage
from tracing import enter, say


def scene(title: str) -> None:
    enter("")
    print(f"\n== {title} ==")


async def until(check: Callable[[], object], what: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            raise RuntimeError(f"等了 {timeout:g} 秒，{what} 还没发生")
        await asyncio.sleep(0.01)


def user_says(db: Conversation, run_id: str, text: str) -> None:
    print(f"\n用户（{run_id}）：「{text}」")
    db.messages.append(HumanMessage(text))


async def main() -> None:
    base = Path(tempfile.mkdtemp(prefix="osb-demo-"))
    storage, redis, network = ObjectStorage(), FakeRedis(), Network()
    controller = Controller(network, storage, base)
    for name, ns, template in (
        ("opensandbox-sbx", "atlas-sbx", SBX_TEMPLATE),
        ("opensandbox-acp", "atlas-acp", ACP_TEMPLATE),
    ):
        network.hosts[name] = LifecycleServer(name, ns, template, controller).handle
    profile = GIT_PROFILES["teamflow-gitee"]
    network.hosts["gitee.com"] = FakeGitee(profile["username"], profile["token"]).handle
    transport = network.transport()
    servers = {
        SBX: LifecycleClient("http://opensandbox-sbx", API_KEYS["opensandbox-sbx"], transport),
        ACP: LifecycleClient("http://opensandbox-acp", API_KEYS["opensandbox-acp"], transport),
    }
    routes = SandboxRoutes(redis)
    ensurer = Ensurer(redis, servers, routes)
    maintainer = Maintainer(ensurer)
    db = Conversation("t1", "u1")
    a = ServerInstance("A", ensurer=ensurer, storage=storage, transport=transport)
    b = ServerInstance("B", ensurer=ensurer, storage=storage, transport=transport)

    def lines(name: str, prefix: str = db.prefix) -> int:
        data = storage.get(prefix + name)
        return data.decode().count("\n") if data else 0

    started = time.monotonic()

    scene("r1 按需建沙箱：路由从对象存储切到沙箱")
    user_says(db, "r1", R1)
    await a.run_turn(db, "r1")

    scene("r2 后台进程与断线：同一个命令 id、同一个游标接上")
    network.cut_once(lambda: lines("ran.log") >= 2, "r2 的测试写到第 2 行时")
    user_says(db, "r2", R2)
    await a.run_turn(db, "r2")
    say(
        "剧本",
        f"对象存储里 ran.log 共 {lines('ran.log')} 行（只跑了一次）；"
        f"progress.log 共 {lines('progress.log')} 行",
    )

    scene("r3 外部写入：界面上传立刻进对象存储，沙箱的挂载要等缓存过期")
    storage.put(db.prefix + "notes.txt", NOTES_V2.encode(), origin="界面上传")
    say("界面", f"用户上传 notes.txt（新版）；界面的文件面板直接读对象存储：{NOTES_V2.strip()!r}")
    user_says(db, "r3", R3)
    await b.run_turn(db, "r3")

    scene("r4 闲置回收：路由先删、沙箱后删；之后读文件不需要沙箱，执行命令再重建")
    await asyncio.sleep(IDLE_TTL_S + 0.1)
    await maintainer.reap_idle()
    user_says(db, "r4", R4)
    await a.run_turn(db, "r4")

    scene("r5 沙箱在命令执行中消失：新沙箱不认识这个命令 → 工具错误 → 模型重跑")
    user_says(db, "r5", R5)
    task = asyncio.create_task(a.run_turn(db, "r5"))
    await until(lambda: lines("reg.log") >= 3, "r5 的回归测试写出 3 行")
    route = routes.get(db.thread_id)
    assert route is not None
    await controller.fail(route.sandbox_id, "节点故障")
    await task
    say(
        "剧本",
        f"对象存储里 reg.log 共 {lines('reg.log')} 行：第一次跑到一半留下的，"
        "加上重跑的 8 行（不撤销已发生的副作用）",
    )

    scene("r6 ACP：opensandbox-acp 建 Pod；模型 key 是模板挂的文件，git 凭据在 Credential Vault")
    enter("r6")
    t2 = AcpThread(
        id="t2",
        workspace_id="t2",
        user_id="u1",
        source_app="teamflow",
        git_profile="teamflow-gitee",
    )
    acp = AcpRuntime(OpenSandboxPods(ensurer), transport)
    reply = await acp.run_turn(t2, "把 RELEASE.md 写好并推到 Gitee")
    say("HostRuntime", f"bridge 回复：{reply}")
    release = storage.get("u1/t2/workspace/RELEASE.md")
    say(
        "界面",
        f"t2 的文件面板（对象存储）：RELEASE.md {'在' if release else '不在'}"
        "（写穿：CLI close 时就上传了）",
    )

    print(f"\n整轮用时约 {time.monotonic() - started:.1f} 秒")
    scene("最终状态")
    print("  run 表")
    for run_id, status in db.runs.items():
        print(f"    {run_id}  {status}")
    print(f"  路由表：{ {ws: routes.get(ws).sandbox_id for ws in routes.workspaces()} }")
    for kind, client in servers.items():
        items = [
            f"{sb['id']}（{sb['status']['state']}，会话 {sb['metadata']['atlas.thread']}）"
            for sb in await client.list({})
        ]
        print(f"  opensandbox-{kind}：{'、'.join(items) or '无'}")
    print(f"  控制器先后起过 {controller.created} 个 Pod")
    for sid, pod in controller.pods.items():
        print(f"  {sid}：{pod.describe()}")
    files = sorted(storage.under(db.prefix))
    print(f"  对象存储 {db.prefix}：{'、'.join(files)}")

    say("剧本", "上次 server 崩溃留下一个没人认领的沙箱（会话 t9）")
    await servers[SBX].create(sbx_spec(ws="t9", user="u9", token="x", image="atlas-sandbox:dev"))
    await maintainer.reap_orphans(live_threads={"t1", "t2"})
    say("剧本", "会话 t1、t2 删除 → release：两种沙箱、路由、端点缓存都清掉")
    await maintainer.release("t1")
    await maintainer.release("t2")
    left = sum([len(await c.list({})) for c in servers.values()])
    print(f"  剩余沙箱 {left} 个，路由 {len(routes.workspaces())} 条")
    shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
