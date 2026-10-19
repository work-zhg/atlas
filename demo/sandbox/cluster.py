"""cluster 服务（全仓唯一有 K8s 写权限的进程）和它背后的 K8s（都是内存模拟）。

    ensure     确保这个会话的沙箱 Pod 在跑：幂等；同一会话串行；Secret 先于 Pod；等到就绪才返回
    release    删 Pod 和 Secret（会话删除时）
    reap_idle  闲置回收：问 sandboxd 有没有命令在跑 —— 有就永远不回收（不设运行上限）

★ 状态从 K8s 里的对象派生，不建 pod 表：两处真相必然漂移。
★ Pod 的键是 workspace_thread_id：子智能体和父会话共用同一个 Pod（工作区本来就是父会话的）。
★ cluster 只交出 Pod 地址和令牌，不碰命令：执行走 server ↔ sandboxd 直连。
"""

from __future__ import annotations

import asyncio
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from pod import SandboxPod
from sandboxd import SESSION_HEADER, LinkFaults
from storage import ObjectStorage
from tracing import say

POD_START_S = 0.3  # 建 Pod 到就绪。Atlas 本机实测约 5 秒（镜像已在本地）
K8S_RT_S = 0.005  # 一次 K8s API 往返
QUOTA_PER_USER = 3  # 每个用户最多几个会话 Pod（沙箱和 ACP 共用）
IDLE_TTL_S = 0.5  # 闲置多久回收。Atlas 是 1800 秒


class ClusterUnavailable(RuntimeError):
    """建不出 Pod（配额满、镜像不允许、K8s 出错）。PodSandbox 把它转成 SandboxUnavailable。"""


class QuotaExceeded(ClusterUnavailable):
    """拒绝而不是排队：排队会让「第一次执行」的延迟不可预测，明确报出来用户可以先关掉旧会话。"""


@dataclass
class PodInfo:
    name: str
    url: str
    token: str
    created: bool


def pod_name(ws: str) -> str:
    return f"atlas-sbx-{ws}"  # ACP 的是 atlas-sess-…


def secret_name(ws: str) -> str:
    return f"atlas-sbx-{ws}-token"


def sandbox_pod_manifest(ws: str, user: str) -> dict[str, Any]:
    """§5 的模板，只留 demo 用得到的字段。"""
    return {
        "name": pod_name(ws),
        "labels": {"atlas/kind": "sandbox", "atlas/thread-id": ws, "atlas/user-id": user},
        "image": "atlas-sandbox:dev",
        "secret": secret_name(ws),  # 令牌经 Secret 卷挂进主容器
        "workspace_prefix": f"{user}/{ws}/workspace/",  # 挂载器挂的对象存储前缀
        "env": {  # 主容器的全部环境变量：没有模型凭据，也没有对象存储凭据
            "HOME": "/home/agent",
            "SANDBOXD_TOKEN_FILE": "/run/secrets/atlas/token",
            "SANDBOXD_SESSION_ID": ws,
        },
    }


class FakeK8s:
    """K8s API（内存模拟）：Secret 和 Pod 两种对象。create_pod 立即返回，Pod 过一会儿才就绪。"""

    def __init__(self, storage: ObjectStorage) -> None:
        self.storage = storage
        self.faults = LinkFaults()  # server ↔ Pod 之间的网络（剧本往里注入故障）
        self.secrets: dict[str, str] = {}
        self.pods: dict[str, SandboxPod] = {}
        self._root = Path(tempfile.mkdtemp(prefix="atlas-sandbox-demo-"))
        self._n = 0
        self._tasks: set[asyncio.Task] = set()

    async def _rt(self) -> None:
        await asyncio.sleep(K8S_RT_S)

    async def apply_secret(self, name: str, token: str) -> None:
        await self._rt()
        self.secrets[name] = token  # 覆盖写

    async def read_secret(self, name: str) -> str | None:
        await self._rt()
        return self.secrets.get(name)

    async def delete_secret(self, name: str) -> None:
        await self._rt()
        self.secrets.pop(name, None)

    async def get_pod(self, name: str) -> SandboxPod | None:
        await self._rt()
        return self.pods.get(name)

    async def list_pods(self) -> list[SandboxPod]:
        await self._rt()
        return list(self.pods.values())

    async def create_pod(self, manifest: dict[str, Any]) -> None:
        await self._rt()
        if manifest["name"] in self.pods:
            raise FileExistsError(manifest["name"])  # 409 AlreadyExists
        self._n += 1
        pod = SandboxPod(manifest, self._root / f"{manifest['name']}-{self._n}")
        self.pods[pod.name] = pod
        task = asyncio.create_task(self._boot(pod))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _boot(self, pod: SandboxPod) -> None:
        """kubelet：拉镜像、跑 init 容器、起 sidecar、起主容器。"""
        await asyncio.sleep(POD_START_S)
        token = self.secrets[pod.manifest["secret"]]  # ★ 容器启动时读的是 Secret 此刻的值
        await asyncio.to_thread(pod.start, token, self.storage, self.faults)
        say("K8s", f"{pod.name} 就绪：sandboxd 监听 {pod.url}，boot {pod.boot_id}")

    async def wait_ready(self, name: str) -> SandboxPod:
        while not (pod := self.pods.get(name)) or not pod.ready:
            if pod is None:
                raise ClusterUnavailable(f"{name} 在就绪前被删了")
            await asyncio.sleep(0.02)
        return pod

    def close(self) -> None:
        shutil.rmtree(self._root, ignore_errors=True)

    async def delete_pod(self, name: str) -> None:
        await self._rt()
        if (pod := self.pods.pop(name, None)) is not None:
            await asyncio.to_thread(pod.delete)


class PodManager:
    def __init__(self, k8s: FakeK8s) -> None:
        self._k8s = k8s
        self._locks: dict[str, asyncio.Lock] = {}  # 真实代码按引用计数回收锁，demo 不回收

    async def ensure(self, *, workspace_thread_id: str, user_id: str) -> PodInfo:
        """确保这个会话的沙箱 Pod 在跑。幂等。

        ★ 同一会话的 ensure 串行。一批并行的 execute 在 Pod 还不存在时会同时到达：
          不串行的话各签一个令牌、各自覆盖写同一个 Secret，只有一个建出 Pod，Pod 启动时读到的是
          最后写入的那份 —— 建 Pod 的那一方手里的令牌就对不上，固定有一个请求 401（实验 3）。
        """
        lock = self._locks.setdefault(workspace_thread_id, asyncio.Lock())
        if lock.locked():
            say("cluster", f"ensure {workspace_thread_id}：同一会话已有 ensure 在进行 → 排队等它")
        async with lock:
            return await self._ensure(workspace_thread_id, user_id)

    async def _ensure(self, ws: str, user: str) -> PodInfo:
        name = pod_name(ws)
        if await self._k8s.get_pod(name) is not None:
            # ★ 复用时不重签令牌：Pod 里注入的是 Secret 里那份，换一个就对不上了
            token = await self._k8s.read_secret(secret_name(ws))
            pod = await self._k8s.wait_ready(name)
            say("cluster", f"ensure {ws}：{name} 已在 → 复用（令牌从 Secret 读回，不重签）")
            return PodInfo(name, pod.url, token or "", created=False)
        await self._check_quota(user)
        token = secrets.token_urlsafe(12)
        say("cluster", f"ensure {ws}：没有 Pod → 签令牌，先写 Secret 再建 Pod {name}")
        # 顺序要紧：Secret 先于 Pod。反过来 Pod 会卡在 ContainerCreating，从外面看只是「起得慢」
        await self._k8s.apply_secret(secret_name(ws), token)
        try:
            await self._k8s.create_pod(sandbox_pod_manifest(ws, user))
        except FileExistsError:  # 并发 ensure 撞上了：对方刚建好，按复用处理
            token = await self._k8s.read_secret(secret_name(ws)) or ""
            pod = await self._k8s.wait_ready(name)
            return PodInfo(name, pod.url, token, created=False)
        started = time.monotonic()
        # ★ 等到就绪才交出地址：Pod 还在 ContainerCreating 时就返回的话，第一次连接必然失败
        pod = await self._k8s.wait_ready(name)
        say("cluster", f"{name} 冷启动 {time.monotonic() - started:.1f} 秒 → 交出地址和令牌")
        return PodInfo(name, pod.url, token, created=True)

    async def _check_quota(self, user: str) -> None:
        mine = [p for p in await self._k8s.list_pods() if p.labels["atlas/user-id"] == user]
        if len(mine) >= QUOTA_PER_USER:
            raise QuotaExceeded(
                f"该用户已有 {len(mine)} 个会话 Pod（上限 {QUOTA_PER_USER}），先关掉一些旧会话再试"
            )

    async def release(self, ws: str) -> None:
        """删 Pod 和它的 Secret。幂等：会话删除的级联会重复调。"""
        await self._k8s.delete_pod(pod_name(ws))
        await self._k8s.delete_secret(secret_name(ws))

    async def reap_idle(self) -> list[str]:
        """闲置回收（设计 §9；真实 cluster 还没实现，是 P2）。周期任务，demo 由剧本手动调。"""
        removed = []
        for pod in await self._k8s.list_pods():
            ws = pod.labels["atlas/thread-id"]
            token = await self._k8s.read_secret(secret_name(ws)) or ""
            async with httpx.AsyncClient(trust_env=False) as client:
                resp = await client.get(
                    f"{pod.url}/v1/info",
                    headers={"Authorization": f"Bearer {token}", SESSION_HEADER: ws},
                )
            info = resp.json()
            if info["running"]:
                n = info["running"]
                say("cluster", f"闲置回收：{pod.name} 有 {n} 条命令在跑 → 不回收（不设运行上限）")
            elif info["idle_s"] <= IDLE_TTL_S:
                say("cluster", f"闲置回收：{pod.name} 才闲置 {info['idle_s']} 秒 → 不回收")
            else:
                idle = info["idle_s"]
                say("cluster", f"闲置回收：{pod.name} 已闲置 {idle} 秒 > {IDLE_TTL_S} → 删 Pod")
                await self.release(ws)
                removed.append(pod.name)
        return removed
