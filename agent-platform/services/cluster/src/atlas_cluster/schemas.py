"""cluster 服务的 API 契约 —— server 与 cluster 之间的那份。

★ 本模块**只依赖 pydantic**：server 要 import 它做类型安全的调用，
  但不该因此拖进 fastapi 与 K8s 客户端。守卫见
  tests/test_cluster_boundary.py 的子进程探测。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

__all__ = [
    "EnsurePodRequest",
    "PodInfo",
    "ReapRequest",
    "ReapResult",
]


class EnsurePodRequest(BaseModel):
    """确保某个会话的 Pod 在跑。

    ★ 两个 thread id 都要：workspace 挂**父会话的**前缀，skills 挂自己的
      （subagent §03）。少传一个就只能由 cluster 猜，而猜错的表现是
      子智能体读不到父的产物、或者读到了父的技能 —— 两个方向都是回归。
    """

    thread_id: str
    user_id: str
    #: acp = CLI Pod（bridge + adapter）；sandbox = 原生智能体的执行沙箱（sandboxd）
    #: （doc/native-sandbox-design.html）。★ 沙箱按工作区建：thread_id 就是 workspace_thread_id，
    #:   子智能体与父会话共用一个。
    kind: Literal["acp", "sandbox"] = "acp"
    #: 工作区归属的会话。主 agent 等于自己，子智能体等于父会话。
    workspace_thread_id: str
    #: Pod 镜像。acp：cli + adapter + bridge 三件套的版本组合；sandbox：沙箱镜像，空 = 默认
    image: str = ""
    #: adapter 启动命令，进 bridge 的 ATLAS_ADAPTER_CMD
    adapter: str = ""
    cli_type: str = ""
    #: git 凭据档案（cluster 配置里的名字）。非空时把该档案的 Secret 只读挂进 Pod，
    #: Pod 内的 git CLI 用它访问代码托管平台。为空 = 不给任何 git 凭据。
    git_profile: str = ""


class PodInfo(BaseModel):
    """连上一个会话 Pod 需要的全部信息。"""

    pod_name: str
    url: str
    #: ★ 随 Pod 生灭：Pod 销毁即失效，重建换新（执行环境 §09）。
    #:   所以它跟着「这次拿到的 Pod」返回，不是从配置读一个长期值。
    token: str
    #: 本次是否新建。复用既有 Pod 时为 False —— 调用方据此判断要不要预热等待。
    created: bool = False


class ReapRequest(BaseModel):
    """孤儿回收：把「还活着的会话」交给 cluster，其余一律清掉。

    ★ 方向刻意是「给活的」而不是「给要删的」：cluster 不认识 thread 表，
      让它自己判断哪个该死会要求它反向查 server —— 那条依赖不该有。
    """

    live_thread_ids: list[str] = Field(default_factory=list)


class ReapResult(BaseModel):
    removed: list[str] = Field(default_factory=list)
