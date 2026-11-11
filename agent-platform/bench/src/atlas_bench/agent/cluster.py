"""集群操作（doc/sandbox-eng-cases.html §4「运行中的动作」）：故障注入与资源回收检查。

只经 kubectl（当前环境的 KUBECONFIG，与 deploy/local/forward.sh 同一份）和 ssh（暂停进程要在
节点上发信号）操作被测部署，不碰平台代码。

    server_pods · owner_pod     server 的 Pod；多副本时查库里的 run.owner
                                （实例 id 以 Pod 名开头）
    kill_server · delete_sandbox
                                强删 Pod（--grace-period=0 --force）：进程直接没了，与崩溃一样；
                                delete_sandbox 也可以优雅删除（容器先收到 SIGTERM）
    rollout_restart             滚动部署 server：旧 Pod 收到 SIGTERM，走停机交接
    pause · resume              在节点上对容器主进程发 SIGSTOP / SIGCONT（假死）
    block · unblock             给沙箱 Pod 下发拒绝全部入站的 NetworkPolicy
                                （server → execd / bridge 断网）
    replicas · scale            OpenSandbox lifecycle server 的副本数
    kill_cli                    在会话 Pod 里结束一个会话的 CLI 进程（它的 bridge 的子进程）
    sandbox_exists · cli_session_alive · pods_with_image
                                回收检查

沙箱 Pod 有两种，在不同的命名空间：原生沙箱（atlas-sbx）、CLI 的会话 Pod（atlas-cli）。按 Pod 名
操作时两边都找（Pod 名由 OpenSandbox 生成，不会重名）。

★ 容器里给 PID 1 发 SIGSTOP 不生效（内核不向本命名空间的 init 投递没有处理函数的信号），所以暂停走
  ssh + crictl 拿到宿主上的 PID 再发。
★ 只读查询与动作都有超时；命令失败抛 ClusterError，由调用方记成「注入没做成」（评测侧错误）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shlex
import uuid
from collections.abc import Awaitable, Callable

from ..settings import BenchSettings

__all__ = ["ClusterError", "ClusterOps", "Runner"]

#: 执行一条命令 → 标准输出。(argv, stdin, 超时秒)；测试里换成假的
Runner = Callable[[list[str], str | None, float], Awaitable[str]]

_CONTAINER_ID = re.compile(r"^[a-z]+://([0-9a-f]{12,})$")


class ClusterError(RuntimeError):
    pass


async def _run(argv: list[str], stdin: str | None, timeout: float) -> str:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(stdin.encode() if stdin is not None else None), timeout=timeout
        )
    except TimeoutError as exc:
        proc.kill()
        raise ClusterError(f"{argv[0]} 超过 {timeout:.0f}s：{shlex.join(argv)}") from exc
    if proc.returncode != 0:
        raise ClusterError(f"{shlex.join(argv)} → {proc.returncode}：{err.decode()[-500:]}")
    return out.decode()


#: 在会话 Pod 里结束一个会话的 CLI：找到这个会话的 bridge（按人共用的 Pod 里每个会话一个，
#: pid 在 /tmp/atlas-bridge/<会话>.pid；一会话一 Pod 的旧布局里只有一个），SIGKILL 它的子进程
#: （adapter）。镜像是 python-slim，没有 pkill，用 python 读 /proc。
_KILL_CLI = r'''
import os, signal, sys
session = sys.argv[1]

def cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        return ""

def parent(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return int(f.read().rsplit(")", 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        return -1

pids = [int(d) for d in os.listdir("/proc") if d.isdigit()]
pidfile = f"/tmp/atlas-bridge/{session}.pid"
if os.path.exists(pidfile):
    bridge = int(open(pidfile).read().strip())
else:
    found = [p for p in pids if "atlas_bridge" in cmdline(p)]
    if len(found) != 1:
        sys.exit(f"找不到会话 {session} 的 bridge：{found}")
    bridge = found[0]
kids = [p for p in pids if parent(p) == bridge]
if not kids:
    sys.exit(f"bridge {bridge} 没有子进程：CLI 不在跑")
for p in kids:
    os.kill(p, signal.SIGKILL)
print(" ".join(f"{p}={cmdline(p)[:60] or '?'}" for p in kids))
'''

#: 会话的 bridge 还在不在：alive / gone；没有 /tmp/atlas-bridge 是一会话一 Pod 的旧布局（legacy），
#: 那里 bridge 就是 Pod 本身，看 Pod 在不在
_BRIDGE_ALIVE = (
    'd=/tmp/atlas-bridge; f="$d/$1.pid"; '
    'if [ ! -d "$d" ]; then echo legacy; '
    'elif [ -f "$f" ] && kill -0 "$(cat "$f")" 2>/dev/null; then echo alive; '
    "else echo gone; fi"
)


class ClusterOps:
    def __init__(self, settings: BenchSettings, *, run: Runner = _run) -> None:
        self._s = settings
        self._run = run
        #: Pod 名 → 所在命名空间（查到就记下）
        self._ns: dict[str, str] = {}

    async def _kubectl(self, *args: str, stdin: str | None = None, timeout: float = 60) -> str:
        return await self._run(["kubectl", *args], stdin, timeout)

    async def _ssh(self, script: str, timeout: float = 30) -> str:
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", self._s.ssh_target, script]
        return await self._run(argv, None, timeout)

    # ------------------------------------------------------------------ server

    async def server_pods(self) -> list[str]:
        """正在运行的 server Pod（不含正在删除的）。"""
        out = await self._kubectl(
            "-n", self._s.k8s_server_namespace, "get", "pods",
            "-l", f"app={self._s.k8s_server_deployment}",
            "--field-selector=status.phase=Running", "-o", "json",
        )  # fmt: skip
        items = json.loads(out).get("items") or []
        return sorted(
            p["metadata"]["name"] for p in items if not p["metadata"].get("deletionTimestamp")
        )

    async def owner_pod(self, run_id: str) -> str:
        """持有这个 run 的 server Pod。单副本就是它；多副本查库里 run.owner。"""
        pods = await self.server_pods()
        if not pods:
            raise ClusterError("没有正在运行的 server Pod")
        if len(pods) == 1:
            return pods[0]
        rid = str(uuid.UUID(run_id))  # 只接受 uuid：要拼进 SQL
        sql = f"select coalesce(owner, '') from run where run_uuid = '{rid}'"
        out = await self._kubectl(
            "-n", self._s.k8s_db_namespace, "exec", self._s.k8s_db_target, "--",
            "psql", "-U", self._s.db_user, "-d", self._s.db_name, "-tAc", sql,
        )  # fmt: skip
        owner = out.strip()
        for pod in pods:
            # 实例 id = 主机名-pid-随机串，主机名就是 Pod 名
            if owner.startswith(f"{pod}-"):
                return pod
        raise ClusterError(f"run {rid} 的持有实例 {owner!r} 不在运行中的 Pod 里：{pods}")

    async def rollout_restart(self) -> None:
        await self._kubectl(
            "-n", self._s.k8s_server_namespace, "rollout", "restart",
            f"deployment/{self._s.k8s_server_deployment}",
        )  # fmt: skip

    async def _host_pid(self, namespace: str, pod: str) -> str:
        out = await self._kubectl(
            "-n", namespace, "get", "pod", pod,
            "-o", "jsonpath={.status.containerStatuses[0].containerID}",
        )  # fmt: skip
        cid = out.strip()
        m = _CONTAINER_ID.match(cid)
        if not m:
            raise ClusterError(f"取不到 {pod} 的容器 id：{cid!r}")
        pid = (
            await self._ssh(
                "crictl inspect --output go-template --template '{{.info.pid}}' "
                + shlex.quote(m.group(1))
            )
        ).strip()
        if not pid.isdigit():
            raise ClusterError(f"取不到 {pod} 在节点上的进程号：{pid!r}")
        return pid

    async def pause(self, pod: str) -> None:
        pid = await self._host_pid(self._s.k8s_server_namespace, pod)
        await self._ssh(f"kill -STOP {pid}")

    async def resume(self, pod: str) -> None:
        pid = await self._host_pid(self._s.k8s_server_namespace, pod)
        await self._ssh(f"kill -CONT {pid}")

    # ------------------------------------------------------------------ Pod 与沙箱

    async def _kill_pod(self, namespace: str, pod: str) -> None:
        await self._kubectl(
            "-n", namespace, "delete", "pod", pod, "--grace-period=0", "--force", "--wait=false"
        )

    def _sandbox_namespaces(self) -> tuple[str, str]:
        return self._s.k8s_sandbox_namespace, self._s.k8s_cli_namespace

    async def _namespace_of(self, pod: str) -> str | None:
        """沙箱 Pod 在哪个命名空间（原生沙箱 / 会话 Pod）；都没有 → None。"""
        if pod in self._ns:
            return self._ns[pod]
        for ns in self._sandbox_namespaces():
            if await self._pod_in(ns, pod):
                self._ns[pod] = ns
                return ns
        return None

    async def _pod_in(self, ns: str, pod: str) -> bool:
        out = await self._kubectl("-n", ns, "get", "pod", pod, "--ignore-not-found", "-o", "name")
        return bool(out.strip())

    async def _sandbox_ns(self, pod: str) -> str:
        ns = await self._namespace_of(pod)
        if ns is None:
            raise ClusterError(f"沙箱 Pod {pod} 不在 {'、'.join(self._sandbox_namespaces())} 里")
        return ns

    async def kill_server(self, pod: str) -> None:
        await self._kill_pod(self._s.k8s_server_namespace, pod)

    async def delete_sandbox(self, pod: str, *, graceful: bool = False) -> None:
        """graceful：按 Pod 的宽限期删（容器先收到 SIGTERM，bridge 以 pod_terminating 收尾这一轮）；
        否则强删（进程直接没了）。都不等删完。"""
        ns = await self._sandbox_ns(pod)
        if graceful:
            await self._kubectl("-n", ns, "delete", "pod", pod, "--wait=false")
        else:
            await self._kill_pod(ns, pod)

    async def sandbox_exists(self, pod: str) -> bool:
        for ns in self._sandbox_namespaces():
            if await self._pod_in(ns, pod):
                return True
        return False

    async def kill_cli(self, pod: str, session: str) -> str:
        """结束会话 Pod 里这个会话的 CLI 进程 → 被结束的进程（pid=命令行）。"""
        ns = await self._sandbox_ns(pod)
        out = await self._kubectl(
            "-n", ns, "exec", pod, "-c", self._s.k8s_sandbox_container, "--",
            "python3", "-c", _KILL_CLI, session,
        )  # fmt: skip
        return out.strip()

    async def cli_session_alive(self, pod: str, session: str) -> bool:
        """会话 Pod 里这个会话的 bridge 还在不在跑（Pod 没了 = 不在）。"""
        ns = await self._namespace_of(pod)
        if ns is None:
            return False
        try:
            out = await self._kubectl(
                "-n", ns, "exec", pod, "-c", self._s.k8s_sandbox_container, "--",
                "sh", "-c", _BRIDGE_ALIVE, "sh", session,
            )  # fmt: skip
        except ClusterError:
            if not await self.sandbox_exists(pod):  # 查的时候正好删掉了
                return False
            raise
        return out.strip() in ("alive", "legacy")

    async def pods_with_image(self, image: str) -> list[str]:
        """会话 Pod 里容器镜像是 image 的（含正在删除的）。镜像名可能被补上了仓库前缀。"""
        out = await self._kubectl("-n", self._s.k8s_cli_namespace, "get", "pods", "-o", "json")
        names = []
        for p in json.loads(out).get("items") or []:
            images = [c.get("image", "") for c in p.get("spec", {}).get("containers") or []]
            if any(i == image or i.endswith("/" + image) for i in images):
                names.append(p["metadata"]["name"])
        return sorted(names)

    async def block(self, pod: str) -> str:
        """拒绝进入这个沙箱 Pod 的全部流量 → 下发的 NetworkPolicy 名字（unblock 用）。"""
        ns = await self._sandbox_ns(pod)
        labels = json.loads(
            await self._kubectl("-n", ns, "get", "pod", pod, "-o", "jsonpath={.metadata.labels}")
            or "{}"
        )
        sandbox = labels.get("atlas.sandbox")
        if not sandbox:
            raise ClusterError(f"沙箱 Pod {pod} 没有 atlas.sandbox 标签")
        name = f"bench-block-{hashlib.sha1(pod.encode()).hexdigest()[:12]}"
        policy = {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": name, "namespace": ns, "labels": {"atlas.bench": "fault"}},
            # 只写 policyTypes: [Ingress]、不写规则 = 拒绝全部入站
            "spec": {"podSelector": {"matchLabels": {"atlas.sandbox": sandbox}},
                     "policyTypes": ["Ingress"]},
        }  # fmt: skip
        await self._kubectl("apply", "-f", "-", stdin=json.dumps(policy))
        return f"{ns}/{name}"

    async def unblock(self, name: str) -> None:
        """name：block 返回的「命名空间/名字」。"""
        ns, _, policy = name.rpartition("/")
        await self._kubectl(
            "-n", ns or self._s.k8s_sandbox_namespace, "delete", "networkpolicy", policy,
            "--ignore-not-found",
        )  # fmt: skip

    # ------------------------------------------------------------------ lifecycle server

    async def replicas(self) -> int:
        out = await self._kubectl(
            "-n", self._s.k8s_opensandbox_namespace, "get",
            f"deployment/{self._s.k8s_lifecycle_deployment}", "-o", "jsonpath={.spec.replicas}",
        )  # fmt: skip
        return int(out.strip() or 0)

    async def scale(self, replicas: int) -> None:
        """缩放并等到位：扩回来时要等新 Pod 就绪，否则下一个 trial 一上来就撞上「不可达」
        （踩过）。"""
        target = f"deployment/{self._s.k8s_lifecycle_deployment}"
        ns = self._s.k8s_opensandbox_namespace
        await self._kubectl("-n", ns, "scale", target, f"--replicas={int(replicas)}")
        if replicas > 0:
            await self._kubectl(
                "-n", ns, "rollout", "status", target, "--timeout=180s", timeout=200
            )

    async def clear_stale_faults(self) -> int:
        """上次评测进程被直接杀掉时没来得及恢复的故障：带 atlas.bench=fault 标签的 NetworkPolicy。

        → 清掉的条数。评测开跑前调。缩掉的副本、暂停的进程不知道原来的样子，只能人工处理。
        """
        cleared = 0
        for ns in self._sandbox_namespaces():
            out = await self._kubectl(
                "-n", ns, "delete", "networkpolicy", "-l", "atlas.bench=fault", "-o", "name"
            )
            # 没有可删的时候 kubectl 也会往标准输出写一句 No resources found，只数真删掉的
            cleared += sum(1 for line in out.splitlines() if line.startswith("networkpolicy"))
        return cleared
