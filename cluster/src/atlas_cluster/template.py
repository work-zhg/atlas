"""Pod 模板渲染（acp 详设 §04 / 文件系统 §05 / 执行环境 §09）。

产出的是 **manifest 字典**而不是 YAML 字符串：字典可以逐字段断言，
字符串只能整段比对 —— 后者一改缩进就红，红了还看不出改坏了什么。
"""

from __future__ import annotations

from typing import Any

from atlas_cluster.config import ClusterSettings
from atlas_cluster.schemas import EnsurePodRequest

__all__ = [
    "LABEL_MANAGED",
    "LABEL_THREAD",
    "LABEL_USER",
    "pod_manifest",
    "pod_name_for",
    "secret_manifest",
    "secret_name_for",
]

#: ★ 状态从 label 派生，**不建 pod 表**。两处真相必然漂移 —— 「哪个会话
#:   有 Pod」查 K8s，「Pod 该不该活着」对照 server 的 thread 表，
#:   reap 的输入就是这两个集合的差（acp 详设 §11）。
LABEL_MANAGED = "atlas/managed-by"
LABEL_THREAD = "atlas/thread-id"
LABEL_USER = "atlas/user-id"

_MANAGED_BY = "atlas-cluster"

#: 容器内的两个挂载点。/workspace 就是会话 OSS 前缀 —— execute 写的文件
#: read_file 读得到，反之亦然（sandbox 协议头部的那条「靠挂载保证」）。
WORKSPACE_MOUNT = "/workspace"
SKILLS_MOUNT = "/skills"
#: CLI 的状态目录（HOME）。★ 必须持久化，否则 resume 只在 Pod 活着时成立 ——
#: 而 Pod 会被闲置回收。真 Claude Code 把会话记录写成
#: ~/.claude/projects/<cwd-slug>/<sessionId>.jsonl，是纯 JSONL 不是 SQLite，
#: 所以挂在对象存储上没有文件锁问题。
STATE_MOUNT = "/state"


def pod_name_for(thread_id: str) -> str:
    """名字由 thread_id 派生 —— 幂等 ensure 靠它，不靠查表。"""
    return f"atlas-sess-{thread_id}".lower()[:63]


def secret_name_for(thread_id: str) -> str:
    return f"atlas-sess-{thread_id}-auth".lower()[:63]


def secret_manifest(req: EnsurePodRequest, token: str, settings: ClusterSettings) -> dict[str, Any]:
    """每 Pod 一份凭证：bridge 的握手 token + 对象存储凭据。

    ★ 用 stringData 而不是 data：省掉一次 base64，也让 kubectl get -o yaml
      的排查输出可读。凭证本身随 Pod 生灭，不做轮换（重建即换新）。

    ★ 对象存储凭据放这里而不是直接写进 Pod spec 的 env.value：manifest 的
      明文谁有 pod read 权限谁就看得见，还会原样躺在 etcd 里。挂载器经
      secretKeyRef 取（见 _mounter_env）。
    """
    data = {"token": token}
    if settings.model_api_key.get_secret_value():
        # adapter 的模型凭据。同样走 Secret —— 它比对象存储凭据更敏感：
        # 拿到它就能以本部署的名义无限量调模型。
        data["model_api_key"] = settings.model_api_key.get_secret_value()
    if settings.workspace_mount == "fuse":
        data["oss_access_key_id"] = settings.oss_access_key_id.get_secret_value()
        data["oss_secret_access_key"] = settings.oss_secret_access_key.get_secret_value()
    elif settings.workspace_mount == "csi":
        # CSI 驱动按自己的约定从 nodePublishSecretRef 取键名。
        data["akId"] = settings.oss_access_key_id.get_secret_value()
        data["akSecret"] = settings.oss_secret_access_key.get_secret_value()

    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": secret_name_for(req.thread_id),
            "namespace": settings.namespace,
            "labels": _labels(req),
        },
        "type": "Opaque",
        "stringData": data,
    }


def pod_manifest(req: EnsurePodRequest, settings: ClusterSettings) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name_for(req.thread_id),
            "namespace": settings.namespace,
            "labels": _labels(req),
        },
        "spec": {
            # ★ 留够 bridge 的三步终止：拒新请求 → 上报中断 → 刷 span。
            #   留不够不是慢，是静默丢事件（Pod 被 SIGKILL 时上报还没发出去）。
            "terminationGracePeriodSeconds": settings.termination_grace_s,
            # 崩了让 K8s 重启 —— bridge 刻意不在进程内自愈（自愈会掩盖崩溃
            # 频率，且半死的 adapter 状态无法验证）。
            "restartPolicy": "Always",
            "automountServiceAccountToken": False,
            # 网络刻意不加 NetworkPolicy —— adapter 就是要出网的（拉依赖、
            # 调模型网关、访问用户给的 API）。入口的唯一闸门是握手上的
            # per-Pod token，不是网络位置：K8s 的网络是平的，「在集群里」
            # 从来不是安全边界（执行环境 §09）。
            "volumes": _volumes(req, settings),
            **_mounters(req, settings),
            "containers": [_bridge_container(req, settings)],
        },
    }


def _labels(req: EnsurePodRequest) -> dict[str, str]:
    return {
        LABEL_MANAGED: _MANAGED_BY,
        LABEL_THREAD: req.thread_id,
        LABEL_USER: req.user_id,
    }


def _volumes(req: EnsurePodRequest, settings: ClusterSettings) -> list[dict[str, Any]]:
    if settings.workspace_mount == "csi":
        return [
            {
                "name": "session-oss",
                "csi": {
                    "driver": settings.oss_csi_driver,
                    "volumeAttributes": {
                        "bucket": settings.oss_bucket,
                        # 只到 user 一层：两个 subPath 在下面各取各的
                        "path": f"/{req.user_id}",
                    },
                    # ★ 驱动要凭据才能访问桶。早先这里是空的 —— 于是挂载
                    #   在任何真集群上都不可能成功，而内存后端不校验 manifest，
                    #   所以单测全绿。
                    "nodePublishSecretRef": {"name": secret_name_for(req.thread_id)},
                },
            },
            {"name": "tmp", "emptyDir": {}},
        ]

    # fuse / none 模式的卷形状是一样的（两个 emptyDir）—— 区别只在
    # none 模式没有挂载器往里挂东西，于是工作区随 Pod 生灭。
    #
    # ★ fuse 模式为什么是两个卷、而不是一个卷加两个 subPath：subPath 是
    #   kubelet 在容器启动时做的 bind mount，**不跟随**后来在卷里建立的
    #   FUSE 挂载。用 subPath 的话主容器看到的会是挂载点被遮住之前的空目录
    #   —— 而且不报错，只是文件"消失"。
    return [
        {"name": "workspace", "emptyDir": {}},
        {"name": "skills", "emptyDir": {}},
        {"name": "state", "emptyDir": {}},
        {"name": "tmp", "emptyDir": {}},
    ]


def _mounters(req: EnsurePodRequest, settings: ClusterSettings) -> dict[str, Any]:
    """fuse 模式的两个挂载器 —— K8s 原生 sidecar。

    ★ 用 initContainers + restartPolicy=Always（原生 sidecar）而不是普通
      容器：它保证**挂载先于 bridge 起来**。顺序反过来的话 adapter 的 cwd
      是个空目录，它会把文件写进去、然后被 FUSE 挂载整个遮住 —— 数据不是
      丢了，是从此谁也看不见，且全程无报错。

    ★ 工作区与技能各一个容器，而不是一个容器跑两个 rclone：一个挂载死了
      要能被 K8s 单独看见并重启。合在一起的话，技能挂载崩了会表现为
      "工作区偶尔卡住"。
    """
    if settings.workspace_mount != "fuse":
        return {}
    return {
        "initContainers": [
            _mounter(
                "oss-workspace",
                req,
                settings,
                volume="workspace",
                # ★ 挂**父会话**的前缀 —— 子智能体与主 agent 共享工作区。
                remote=f"{req.user_id}/{req.workspace_thread_id}/workspace",
                read_only=False,
            ),
            _mounter(
                "oss-state",
                req,
                settings,
                volume="state",
                # ★ 挂**自己的** system 前缀 —— CLI 的会话记录跟着 agent 走，
                #   和工作区一样不能跟父会话共享（两个 agent 的对话历史混在
                #   一起，resume 出来的上下文就是错的）。
                remote=f"{req.user_id}/{req.thread_id}/system/cli-home",
                read_only=False,
            ),
            _mounter(
                "oss-skills",
                req,
                settings,
                volume="skills",
                # ★ 挂**自己的**前缀，只读 —— 技能跟着 agent 走。两个前缀来自
                #   不同 thread，这正是"工作区共享、技能隔离"之所以是结构性的。
                remote=f"{req.user_id}/{req.thread_id}/skills",
                read_only=True,
            ),
        ]
    }


def _mounter(
    name: str,
    req: EnsurePodRequest,
    settings: ClusterSettings,
    *,
    volume: str,
    remote: str,
    read_only: bool,
) -> dict[str, Any]:
    target = f"/mnt/{volume}"
    flags = [
        "--allow-other",  # bridge 以非 root 跑，不给就看不见挂载内容
        # ★ 不可省。挂载点本身就是那个共享卷的 mountpoint，rclone 默认会
        #   因为"目录已是挂载点"直接拒绝启动（CRITICAL: directory already
        #   mounted）。整个 Pod 因此卡在 Init:CrashLoopBackOff。
        "--allow-non-empty",
        "--vfs-cache-mode writes",  # 对象存储不支持随机写，靠本地缓存补
        # ★ 缩短回写窗口（默认 5s）。文件关闭到真正上传之间的这段时间里，
        #   数据只在容器本地 —— Pod 这时被杀就丢了。对 CLI 的会话记录来说，
        #   丢的表现是**下一轮悄悄失忆**：resume 成功，但少了最后一轮。
        #   缩不到零：真要零窗口得放弃缓存，而对象存储不支持随机写。
        "--vfs-write-back 1s",
        "--dir-cache-time 5s",  # server 经 API 写的文件要能较快被 Pod 看见
        "--file-perms 0666",
        "--dir-perms 0777",
    ]
    if read_only:
        flags.append("--read-only")

    return {
        "name": name,
        "image": settings.mounter_image,
        # ★ 原生 sidecar 的标志：在 initContainers 里但永远不退出。
        "restartPolicy": "Always",
        "command": ["/bin/sh", "-c"],
        "args": [
            # ★ 先拆旧挂载再挂。传播是 Bidirectional，所以挂载会留在宿主的
            #   挂载命名空间里 —— 挂载器一旦重启（崩溃、OOM、节点抖动），
            #   旧的那个还在，新的就会一层层叠上去。失败两次就叠出一堆
            #   谁也读不到正确内容的影子挂载。`|| true`：第一次启动时没有
            #   东西可拆，那不是错误。
            f"fusermount -uz {target} 2>/dev/null || true\n"
            f"mkdir -p {target}\n"
            f'exec rclone mount "oss:$OSS_BUCKET/{remote}" {target} {" ".join(flags)}\n'
        ],
        "env": _mounter_env(req, settings),
        "securityContext": {
            # ★ FUSE 要 /dev/fuse，挂载传播到宿主再进 bridge 容器要
            #   Bidirectional，而 Bidirectional 是 K8s 明确要求 privileged 的。
            #   这是 fuse 模式的代价，也是 csi 模式存在的理由。
            "privileged": True,
        },
        "volumeMounts": [
            {"name": volume, "mountPath": target, "mountPropagation": "Bidirectional"}
        ],
        # ★ 挂上了才放 bridge 起来。没有这个探针，"原生 sidecar 已启动"
        #   只代表进程拉起来了，rclone 可能还在跟对象存储握手。
        "startupProbe": {
            "exec": {"command": ["/bin/sh", "-c", f"grep -q ' {target} ' /proc/mounts"]},
            "periodSeconds": 1,
            "failureThreshold": 60,
        },
        "resources": {
            "requests": {
                "cpu": settings.mounter_cpu_request,
                "memory": settings.mounter_memory_request,
            },
            "limits": {"memory": settings.mounter_memory_limit},
        },
    }


def _mounter_env(req: EnsurePodRequest, settings: ClusterSettings) -> list[dict[str, Any]]:
    """★ 对象存储凭据走 Secret 引用，且**只给挂载器**，不给 bridge。

    两层，各挡一件事：

      ① 不写进 manifest 的明文。Pod spec 谁有 pod read 权限谁就看得见，
        还会原样躺在 etcd 与各种 `kubectl get -o yaml` 的排查输出里。
        token 一直是这么做的，凭据没理由例外。

      ② 不进 bridge 容器。bridge 里跑的是 adapter，而 adapter 会执行模型
        生成的命令 —— 凭据只要出现在它的环境里，一条 `env` 就能读走。

    需要说清楚的是：这把 key 的权限是**整桶**的，不是本会话前缀的。
    所以隔离靠的是"凭据不在能执行任意命令的容器里"，而不是凭据本身受限。
    真要做到前缀级最小权限，得换成按会话签发的临时凭据（STS / 下放式签名），
    那是另一件事，现在没做。
    """
    secret = secret_name_for(req.thread_id)

    def _from_secret(env_name: str, key: str) -> dict[str, Any]:
        return {
            "name": env_name,
            "valueFrom": {"secretKeyRef": {"name": secret, "key": key}},
        }

    return [
        {"name": "RCLONE_CONFIG_OSS_TYPE", "value": "s3"},
        {"name": "RCLONE_CONFIG_OSS_PROVIDER", "value": settings.oss_provider},
        {"name": "RCLONE_CONFIG_OSS_ENDPOINT", "value": settings.oss_endpoint},
        # ★ region 必须显式给：SigV4 拿它参与签名，而 GCS 不按 AWS 区域划分。
        {"name": "RCLONE_CONFIG_OSS_REGION", "value": settings.oss_region},
        {"name": "OSS_BUCKET", "value": settings.oss_bucket},
        _from_secret("RCLONE_CONFIG_OSS_ACCESS_KEY_ID", "oss_access_key_id"),
        _from_secret("RCLONE_CONFIG_OSS_SECRET_ACCESS_KEY", "oss_secret_access_key"),
    ]


def _adapter_env(req: EnsurePodRequest, settings: ClusterSettings) -> list[dict[str, Any]]:
    """真 CLI 需要的环境：模型端点、凭据、状态目录。

    ★ 早先这里什么都没有 —— 模板只注入 ATLAS_*，于是**真 CLI 根本没法
      认证**。假 adapter 不调模型，所以这个缺口一路没被发现。

    ★ 变量名按 cli_type 分：claude-code 认 ANTHROPIC_*，别的 CLI 各有各的
      一套。不认识的 cli_type 就只给 HOME —— 宁可让它因为缺凭据明确失败，
      也不要塞一堆它不认的变量进去。

    ★ base_url 指向任何 Anthropic 兼容端点即可（已验证 DeepSeek 的
      /anthropic 能驱动真 Claude Code），所以这里不写死厂商。
    """
    secret = secret_name_for(req.thread_id)
    # ★ HOME 必须可写**且持久**：根是只读的，而 CLI 把会话记录写在 HOME 下 ——
    #   放 emptyDir 的话 resume 只在 Pod 活着时成立，而 Pod 会被闲置回收。
    env: list[dict[str, Any]] = [{"name": "HOME", "value": STATE_MOUNT}]

    if req.cli_type != "claude-code" or not settings.model_base_url:
        return env

    env.append({"name": "ANTHROPIC_BASE_URL", "value": settings.model_base_url})
    if settings.model_name:
        env.append({"name": "ANTHROPIC_MODEL", "value": settings.model_name})
        # 小模型也指过去 —— 不指的话 CLI 会去找一个端点上不存在的 haiku，
        # 那条路的失败发生在生成标题之类的旁路上，很难联想到模型配置。
        env.append({"name": "ANTHROPIC_SMALL_FAST_MODEL", "value": settings.model_name})
    if settings.model_api_key.get_secret_value():
        env.append(
            {
                "name": "ANTHROPIC_AUTH_TOKEN",
                "valueFrom": {"secretKeyRef": {"name": secret, "key": "model_api_key"}},
            }
        )
    return env


def _bridge_mounts(req: EnsurePodRequest, settings: ClusterSettings) -> list[dict[str, Any]]:
    """三个挂载点。★ 无论哪种模式，语义都必须一样 ——
    /workspace 是**父会话的**（共享），/skills 与 /state 是**自己的**，
    其中 /skills 只读、/state 是 CLI 的 HOME。
    """
    tmp = {"name": "tmp", "mountPath": "/tmp"}  # noqa: S108

    if settings.workspace_mount == "csi":
        return [
            {
                "name": "session-oss",
                "mountPath": WORKSPACE_MOUNT,
                "subPath": f"{req.workspace_thread_id}/workspace",
            },
            {
                "name": "session-oss",
                "mountPath": SKILLS_MOUNT,
                "subPath": f"{req.thread_id}/skills",
                "readOnly": True,
            },
            {
                # CLI 的 HOME。★ csi 模式下它落在 emptyDir 上，随 Pod 生灭 ——
                #   也就是说**这个模式下 resume 撑不过 Pod 重建**。
                #   CSI 驱动各家的 subPath 语义不一，没法像 fuse 那样再挂
                #   一份 system 前缀，这是这个模式已知的短板。
                "name": "session-oss",
                "mountPath": STATE_MOUNT,
                "subPath": f"{req.thread_id}/system/cli-home",
            },
            tmp,
        ]

    # fuse 模式：挂载器已经把各自的前缀挂好了，这里只要把挂载**传播**进来。
    #
    # ★ mountPropagation=HostToContainer 不可省：没有它，容器拿到的是卷在
    #   启动瞬间的视图，FUSE 挂载是在那之后建立的 —— 目录会一直是空的，
    #   而且不报错。
    return [
        {
            "name": "workspace",
            "mountPath": WORKSPACE_MOUNT,
            "mountPropagation": "HostToContainer",
        },
        {
            # CLI 的 HOME —— 持久化才有 resume（见 _adapter_env）
            "name": "state",
            "mountPath": STATE_MOUNT,
            "mountPropagation": "HostToContainer",
        },
        {
            "name": "skills",
            "mountPath": SKILLS_MOUNT,
            "mountPropagation": "HostToContainer",
            # rclone 侧已经是 --read-only；这里再钉一道，两层都说同一件事。
            "readOnly": True,
        },
        tmp,
    ]


def _bridge_container(req: EnsurePodRequest, settings: ClusterSettings) -> dict[str, Any]:
    return {
        "name": "bridge",
        "image": req.image,
        "ports": [{"containerPort": settings.bridge_port, "name": "bridge"}],
        "env": [
            {
                # ★ 凭证从 Secret 注入，不进镜像也不进 manifest 的明文。
                "name": "ATLAS_BRIDGE_TOKEN",
                "valueFrom": {
                    "secretKeyRef": {"name": secret_name_for(req.thread_id), "key": "token"}
                },
            },
            # 会话绑定：bridge 只接受针对**本 Pod 所属会话**的指令。
            {"name": "ATLAS_THREAD_ID", "value": req.thread_id},
            {"name": "ATLAS_ADAPTER_CMD", "value": req.adapter},
            {"name": "ATLAS_BRIDGE_PORT", "value": str(settings.bridge_port)},
            {"name": "ATLAS_ADAPTER_CWD", "value": WORKSPACE_MOUNT},
            *_adapter_env(req, settings),
        ],
        "volumeMounts": _bridge_mounts(req, settings),
        # ★ 没有这个探针，Pod 的 Ready 只代表"容器进程起来了"——
        #   而 bridge 还要拉起 adapter、握完 initialize 才会 bind 端口。
        #   ensure 在那个窗口里把地址交出去，server 连过去就是
        #   ECONNREFUSED。真集群上这是个**稳定复现**的竞态：Python 启动
        #   加 adapter 派生要好几秒，比 kubelet 标 Ready 慢得多。
        "readinessProbe": {
            "tcpSocket": {"port": settings.bridge_port},
            "periodSeconds": 1,
            "failureThreshold": 60,
        },
        "resources": {
            "requests": {"cpu": settings.cpu_request, "memory": settings.memory_request},
            "limits": {"cpu": settings.cpu_limit, "memory": settings.memory_limit},
        },
        "securityContext": {
            "runAsNonRoot": True,
            # ★ runAsUser 不可省。只写 runAsNonRoot 的话，kubelet 没法在启动前
            #   判定镜像是不是 root —— 除非镜像自己在 USER 里写了**数字** UID，
            #   否则一律拒绝启动，报 CreateContainerConfigError。也就是说
            #   "镜像必须声明数字 USER" 会变成一条不成文的镜像要求，
            #   而它的违反方式是 Pod 根本起不来。
            #   显式给 UID 就没有这个要求了：挂载那边是 0666/0777 + allow-other，
            #   任何 UID 都读写得了。
            "runAsUser": settings.run_as_user,
            "runAsGroup": settings.run_as_user,
            "allowPrivilegeEscalation": False,
            # ★ 根只读：可写区只有挂进来的 /workspace 与 /tmp。
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        },
    }
