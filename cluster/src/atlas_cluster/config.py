"""cluster 服务的配置。"""

from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["ClusterSettings"]


class ClusterSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ATLAS_CLUSTER_", extra="ignore")

    namespace: str = "atlas-sessions"
    #: bridge 在 Pod 内监听的端口
    bridge_port: int = 8900
    #: 每租户的 Pod 上限。★ 没有配额，一个租户的批量会话会把集群占满
    #: （执行环境 §09）。
    quota_per_user: int = 10
    #: 闲置多久回收。与旧沙箱同语义：到点删，不是停 —— Pod 不像容器那样
    #: 能廉价地停着不动。
    idle_ttl_s: int = 1800

    #: 等上一个同名 Pod 终止完的上限。★ 要盖过 termination_grace_s ——
    #: 否则「删了会话立刻又开一个」会撞 409。
    terminating_wait_s: float = 40.0

    #: 优雅终止窗口。★ 要留够 bridge 做三件事：拒新请求、上报中断、刷 span
    #: （执行环境 §09）。留不够的后果不是慢，是**静默丢事件**。
    termination_grace_s: int = 30

    #: 等 Pod Ready 的上限。★ ensure **必须**等 —— 不等的话 URL 与 token
    #: 在 Pod 还处于 ContainerCreating 时就交给了 server，而 AcpChannel 的
    #: 连接超时只有 10s 且不重试：第一轮必然报"连不上 bridge"。
    pod_ready_timeout_s: float = 120.0

    # ── 会话工作区的对象存储 ────────────────────────────────────────
    #
    # 两种挂法，由 workspace_mount 选：
    #
    #   "fuse"（默认）—— 每 Pod 起一个 rclone sidecar，用 S3 兼容协议把桶的
    #     会话前缀挂进来。不需要集群管理员预装任何 CSI 驱动，因此在 k3s、
    #     自建 K8s、GKE 上都一样能跑；凭据就是 server 侧 OssFilesystem 用的
    #     那一份 HMAC key（一套凭据两个消费者，不会漂移）。
    #
    #   "csi" —— 用托管云自带的驱动（GKE 的 gcsfuse、阿里云的 OSS 插件）。
    #     省掉 sidecar，但驱动是云厂商绑定的：GKE 的 gcsfuse CSI 装不到
    #     非 GKE 集群上。
    #
    #   "none" —— 不挂对象存储，工作区是随 Pod 生灭的 emptyDir。对应 server
    #     侧 workspace_configured=False 的那种部署（没配对象存储就没有文件
    #     能力）。Pod 生命周期的测试也用它 —— 那些用例测的是调度与回收，
    #     不该因为缺一套桶凭据就跑不起来。
    #
    # ★ 默认选 fuse 是因为它**可验证**：CSI 那条路在本地集群上根本跑不起来，
    #   而跑不起来的路径等于没测过。
    workspace_mount: str = "fuse"

    oss_bucket: str = ""
    oss_endpoint: str = ""
    oss_region: str = "auto"
    #: rclone 的 S3 provider 名。GCS / Alibaba / Minio / Other ——
    #: 它决定 rclone 绕开哪些厂商特有的怪癖。
    oss_provider: str = "GCS"
    oss_access_key_id: SecretStr = SecretStr("")
    oss_secret_access_key: SecretStr = SecretStr("")

    #: fuse 模式的挂载器镜像。
    mounter_image: str = "docker.io/rclone/rclone:latest"
    #: 挂载器的资源 —— 它只做 IO 转发，给多了是浪费每个会话的配额。
    mounter_cpu_request: str = "50m"
    mounter_memory_request: str = "128Mi"
    mounter_memory_limit: str = "512Mi"

    #: CSI 模式用。仅在 workspace_mount="csi" 时有意义。
    oss_csi_driver: str = "ossplugin.csi.alibabacloud.com"

    # ── adapter（真 CLI）的模型接入 ──────────────────────────────
    #
    # ★ 放在 cluster 配置里而不是 agent spec 里：spec 会被 API 原样返回，
    #   凭据进去就等于公开。这与 MCP server 的凭据用 ${ENV_VAR} 占位符
    #   是同一条纪律。
    #
    # ★ base_url 指向任何 Anthropic 兼容端点即可 —— 已验证 DeepSeek 的
    #   /anthropic 能驱动真 Claude Code。
    model_base_url: str = ""
    model_api_key: SecretStr = SecretStr("")
    model_name: str = ""

    #: bridge 容器以哪个 UID 跑。★ 必须是具体数字 —— 见 template 里
    #: runAsUser 的注释（只有 runAsNonRoot 会让大多数镜像起不来）。
    run_as_user: int = 1000

    cpu_request: str = "250m"
    cpu_limit: str = "2"
    memory_request: str = "512Mi"
    memory_limit: str = "4Gi"
