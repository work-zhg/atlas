"""PodManager —— 会话 Pod 的生命周期。

三条纪律沿用已验证过一轮的 SandboxManager（docs/sandbox.md）：

  ① 状态从 label 派生，**不建 pod 表** —— 两处真相必然漂移
  ② ensure 幂等 —— 名字由 thread_id 派生，重复调复用同一个 Pod
  ③ reap 以「还活着的会话」为输入 —— cluster 不认识 thread 表
"""

from __future__ import annotations

import asyncio
import logging
import time

from atlas_cluster.backend import ClusterBackend
from atlas_cluster.config import ClusterSettings
from atlas_cluster.pairing import issue_token
from atlas_cluster.schemas import EnsurePodRequest, PodInfo
from atlas_cluster.template import (
    LABEL_KIND,
    LABEL_MANAGED,
    LABEL_THREAD,
    LABEL_USER,
    pod_manifest,
    pod_name_for,
    sandbox_pod_manifest,
    secret_manifest,
    secret_name_for,
)

logger = logging.getLogger(__name__)

__all__ = ["ImageDenied", "PodManager", "QuotaExceeded"]

_KINDS = ("acp", "sandbox")

_MANAGED_SELECTOR = f"{LABEL_MANAGED}=atlas-cluster"


class QuotaExceeded(RuntimeError):
    """租户的 Pod 数到顶。

    ★ 拒绝而不是排队：排队会让「新建会话」这个动作的延迟变得不可预测，
      而用户看到的是界面转圈。明确报出来，他们可以先关掉几个旧会话。
    """


class GitProfileDenied(PermissionError):
    """请求的 git 凭据档案不存在，或会话创建者不在该档案的允许名单里。"""


class ImageDenied(PermissionError):
    """沙箱镜像不在允许范围内（sandbox_image_allow_prefixes）。"""


def _kind_of(pod) -> str:
    # 老的 ACP Pod 没有 kind label
    return pod.labels.get(LABEL_KIND, "acp")


class PodManager:
    def __init__(self, backend: ClusterBackend, settings: ClusterSettings) -> None:
        self._backend = backend
        self._settings = settings
        #: (thread_id, kind) → [锁, 持有或等待它的请求数]。同一会话 Pod 的 ensure 串行，见 ensure
        self._locks: dict[tuple[str, str], list] = {}

    async def ensure(self, req: EnsurePodRequest) -> PodInfo:
        """确保该会话的 Pod 在跑。幂等。

        ★ 复用时**不重发 token**：现有 Pod 里的 Secret 已经注入过了，
          换一个新的只会让它与 Pod 内的那份对不上。token 因此存在
          Secret 里，复用路径经 backend.read_secret_value 读回来。

        ★ 同一会话 Pod 的 ensure **串行**。并发的 ensure（一批并行的 execute 在 Pod 还
          不存在时同时到达）会各签一个 token、各自**覆盖写**同一个 Secret；只有一个建出
          Pod，而 Pod 启动时读到的是最后写入的那份 —— 建 Pod 的那一方手里的 token
          就对不上，固定有一个请求 401（2026-10-13 评测 atlas-eng/parallel-approval 撞到）。
          cluster 是单副本（全仓唯一持有 K8s 写权限的进程），进程内加锁即可。
        """
        key = (req.thread_id, req.kind)
        entry = self._locks.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                return await self._ensure(req)
        finally:
            # ★ 按引用计数回收，不看 lock.locked()：锁释放后、下一个等待者拿到之前，
            #   locked() 是 False —— 那时删掉的话新来的请求会建一把新锁，又并发了
            entry[1] -= 1
            if entry[1] == 0:
                del self._locks[key]

    async def _ensure(self, req: EnsurePodRequest) -> PodInfo:
        self._check_git_profile(req)
        self._check_image(req)
        name = pod_name_for(req.thread_id, req.kind)
        await self._await_terminating(req.thread_id, req.kind)
        existing = await self._find(req.thread_id, req.kind)
        if existing is not None:
            token = await self._token_of(req.thread_id, req.kind)
            # ★ 复用也要等就绪：Pod 可能刚被 K8s 重启过（adapter 崩了），
            #   此刻它在 list 里、不是 terminating，但还没在监听。
            return await self._ready_info(name, token, created=False, kind=req.kind)

        await self._check_quota(req.user_id)

        token = issue_token()
        # 顺序要紧：Secret 先于 Pod。反过来的话 Pod 会因为引用不到 Secret
        # 卡在 ContainerCreating，而那个状态从外面看只是「起得慢」。
        render = sandbox_pod_manifest if req.kind == "sandbox" else pod_manifest
        manifest = render(req, self._settings)  # 先渲染：渲染失败不留下孤儿 Secret
        await self._backend.apply_secret(secret_manifest(req, token, self._settings))
        try:
            await self._backend.create_pod(manifest)
        except FileExistsError:
            # 并发 ensure 撞上了 —— 对方刚建好，按复用处理。
            token = await self._token_of(req.thread_id, req.kind)
            return await self._ready_info(name, token, created=False, kind=req.kind)
        logger.info("为会话 %s 创建 %s Pod %s", req.thread_id, req.kind, name)
        return await self._ready_info(name, token, created=True, kind=req.kind)

    async def _ready_info(
        self, name: str, token: str, *, created: bool, kind: str = "acp"
    ) -> PodInfo:
        """等到 Pod 真的能连，再把地址交出去。

        ★ 这一步早先没有 —— ensure 在 Pod 还处于 ContainerCreating 时就返回了。
          后果不是"慢一点"：HostClient 的连接超时只有 10s 且不重试，所以
          **每个新会话的第一轮都必然失败**，报的还是"连不上 bridge"——
          一个看起来像网络问题的调度问题。内存后端里 create 即就绪，
          所以单测一路绿灯。
        """
        pod = await self._backend.wait_ready(name, timeout_s=self._settings.pod_ready_timeout_s)
        url = (
            f"http://{pod.ip}:{self._settings.sandboxd_port}"
            if kind == "sandbox"
            else self._url(pod.ip)
        )
        return PodInfo(pod_name=name, url=url, token=token, created=created)

    def _check_git_profile(self, req: EnsurePodRequest) -> None:
        """★ 档案是凭据：谁能用由 cluster 决定，不信任调用方 —— 名字写错或无权一律拒绝，
        而不是静默不挂（静默的表现是 git 在 Pod 里鉴权失败，原因很难追到这里）。"""
        if not req.git_profile:
            return
        profile = self._settings.git_profiles.get(req.git_profile)
        if profile is None:
            raise GitProfileDenied(f"git 凭据档案 {req.git_profile!r} 不存在")
        if profile.allowed_users and req.user_id not in profile.allowed_users:
            raise GitProfileDenied(f"会话创建者无权使用 git 凭据档案 {req.git_profile!r}")
        if profile.allowed_apps and req.source_app not in profile.allowed_apps:
            apps = "、".join(profile.allowed_apps)
            raise GitProfileDenied(f"只有 {apps} 建的会话能用 git 凭据档案 {req.git_profile!r}")

    def _check_image(self, req: EnsurePodRequest) -> None:
        """★ 沙箱镜像由 cluster 把关：任意镜像 = 任意代码以会话身份跑在集群里。"""
        if req.kind != "sandbox" or not req.image:
            return
        if req.image == self._settings.sandbox_default_image:
            return
        if not any(req.image.startswith(p) for p in self._settings.sandbox_image_allow_prefixes):
            raise ImageDenied(f"沙箱镜像 {req.image!r} 不在允许范围内")

    async def release(self, thread_id: str) -> None:
        """删 Pod 与它的 Secret（两种 kind 都删）。幂等 —— 会话删除的级联会重复调。"""
        for kind in _KINDS:
            await self._backend.delete_pod(pod_name_for(thread_id, kind))
            await self._backend.delete_secret(secret_name_for(thread_id, kind))

    async def reap(self, live_thread_ids: list[str]) -> list[str]:
        """清掉不在「活会话」名单里的 Pod。

        ★ 输入是活的而不是要删的：cluster 不认识 thread 表，让它自己判断
          哪个该死会要求它反向查 server —— 那条依赖不该有。
        """
        live = set(live_thread_ids)
        removed: list[str] = []
        young_before = time.time() - self._settings.reap_min_age_s
        for pod in await self._backend.list_pods(label_selector=_MANAGED_SELECTOR):
            thread_id = pod.labels.get(LABEL_THREAD, "")
            if pod.created_at is not None and pod.created_at > young_before:
                continue  # 太年轻：可能是名单取完之后才建的会话
            if thread_id and thread_id not in live:
                await self.release(thread_id)
                removed.append(thread_id)
        if removed:
            logger.warning("回收了 %d 个孤儿 Pod", len(removed))
        return removed

    # ------------------------------------------------------------------ 内部

    async def _find(self, thread_id: str, kind: str = "acp"):
        """找一个**可用**的 Pod。正在终止的不算 —— 连上去才发现它在关闭。"""
        for pod in await self._backend.list_pods(label_selector=self._selector(thread_id)):
            if not pod.terminating and _kind_of(pod) == kind:
                return pod
        return None

    async def _await_terminating(self, thread_id: str, kind: str = "acp") -> None:
        """等上一个同名 Pod 真的消失。

        ★ K8s 里同名 Pod 在旧的完全删除前建不出来（409）。而 delete 是
          异步的：release 返回之后它还要优雅终止一段（我们给了
          terminationGracePeriodSeconds 让 bridge 收尾）。不等就重建的话，
          「删了会话立刻又开一个」这个很常见的动作会撞 409 而失败。
        ★ 等不到就放行：让 create 的 409 去报错，那里的信息更具体。
        """
        deadline = time.monotonic() + self._settings.terminating_wait_s
        while time.monotonic() < deadline:
            pods = await self._backend.list_pods(label_selector=self._selector(thread_id))
            if not any(pod.terminating and _kind_of(pod) == kind for pod in pods):
                return
            await asyncio.sleep(0.5)
        logger.warning("会话 %s 的旧 Pod 仍在终止中，继续尝试创建", thread_id)

    def _selector(self, thread_id: str) -> str:
        return f"{_MANAGED_SELECTOR},{LABEL_THREAD}={thread_id}"

    async def _check_quota(self, user_id: str) -> None:
        selector = f"{_MANAGED_SELECTOR},{LABEL_USER}={user_id}"
        current = len(await self._backend.list_pods(label_selector=selector))
        if current >= self._settings.quota_per_user:
            msg = (
                f"该用户已有 {current} 个会话 Pod（上限 "
                f"{self._settings.quota_per_user}）。先关掉一些旧会话再试。"
            )
            raise QuotaExceeded(msg)

    async def _token_of(self, thread_id: str, kind: str = "acp") -> str:
        """从既有 Secret 读回 token。

        ★ 走协议而不是偷看 backend 的属性。早先的实现读的是
          `backend.secrets` —— 那是内存实现独有的，真集群上恒读不到，
          于是每次复用都静默重签一个新 token，而 Pod 里注入的还是旧的：
          bridge 握手必然被拒。真集群冒烟第一下就撞出来了。

        读不到时重新签发 —— 这条路只在「Secret 被手工删了」这种异常状态
        下走到，重发至少能让 Pod 重启后恢复可用，比直接失败好。
        """
        token = await self._backend.read_secret_value(secret_name_for(thread_id, kind), "token")
        if token:
            return token
        return await self._reissue(thread_id, kind)

    async def _reissue(self, thread_id: str, kind: str = "acp") -> str:
        logger.warning("会话 %s 的配对 Secret 丢失，重新签发", thread_id)
        token = issue_token()
        await self._backend.apply_secret(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {
                    "name": secret_name_for(thread_id, kind),
                    "namespace": self._settings.namespace,
                    "labels": {LABEL_MANAGED: "atlas-cluster", LABEL_THREAD: thread_id},
                },
                "type": "Opaque",
                "stringData": {"token": token},
            }
        )
        return token

    def _url(self, pod_ip: str) -> str:
        """用 **Pod IP**，不用 DNS 名。

        ★ 早先这里返回的是 `ws://<pod>.<ns>:8900`，注释说"由 cluster 侧的
          Service 兜底"—— 而那个 Service 从来没有被创建过。也就是说 server
          拿到的一直是个**解析不了**的地址。内存后端只比对字符串，
          真集群上这条路从第一次连接就是断的。

          Pod IP 不需要任何额外对象，而且 ensure 已经等到就绪了，此刻它
          一定有值。代价是 Pod 重建后 IP 会变 —— 但每次 run 都重新 ensure，
          拿到的就是新的。
        """
        return f"ws://{pod_ip}:{self._settings.bridge_port}"
