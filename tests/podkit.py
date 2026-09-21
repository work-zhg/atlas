"""对着真集群跑会话 Pod 的测试装配。

bridge 的生产镜像本机构建不了（开发机上没有任何镜像构建工具），所以这里
用 python:3.14-slim 加 hostPath 把仓库源码与 venv 挂进去，adapter 用
tests/fake_adapter.py。

★ 被替换掉的**只有"镜像怎么来"**。Pod、kubelet 的准入、挂载与传播、
  securityContext、Secret 引用、WS、JSON-RPC 全都是真的 —— 否则这些
  测试就测不到它们了，而它们正是内存后端掩盖过的那一批。

★ 明确不覆盖：镜像构建本身。生产镜像是否装对了依赖、入口点是否正确，
  这里一个字都没验。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 与宿主 venv 同版本 —— 不然 pydantic_core 这类带 C 扩展的包 ABI 对不上。
BRIDGE_IMAGE = "docker.io/library/python:3.14-slim"
VENV_SITE_PACKAGES = REPO_ROOT / ".venv" / "lib" / "python3.14" / "site-packages"

#: bridge 在容器里拉起的 adapter。
FAKE_ADAPTER_CMD = "python /opt/atlas/tests/fake_adapter.py"

# ── 真 Claude Code CLI ────────────────────────────────────────────────
#
# 两条路，优先用第一条：
#
#   ① 生产镜像（docker/bridge/Dockerfile）—— node、adapter、bridge 都在
#     镜像里，Pod 不需要任何 hostPath。这是**被部署的那个东西**。
#   ② hostPath 拼装 —— 镜像没构建时的回落。它能验链路，但验不到镜像本身：
#     依赖装没装对、入口点对不对、UID 是不是数字，全都绕过去了。
PROD_IMAGE = "docker.io/library/atlas-acp-bridge:0.1.0"
#: 镜像内 adapter 的位置（与 Dockerfile 里的 ATLAS_ADAPTER_CMD 一致）。
PROD_ADAPTER_CMD = (
    "node /opt/acp-cli/lib/node_modules/@zed-industries/claude-code-acp/dist/index.js"
)

# 回落路径用的宿主目录
NODE_HOME = Path("/opt/node")
CLI_HOME = Path("/opt/acp-cli")
REAL_ADAPTER_CMD = (
    f"{NODE_HOME}/bin/node {CLI_HOME}/node_modules/@zed-industries/claude-code-acp/dist/index.js"
)


def prod_image_available() -> bool:
    """生产镜像在本节点的 containerd 里吗。

    ★ 查而不是试着拉：这个镜像是本地构建后 import 进去的，registry 上
      根本没有，拉一定失败。

    ★ 必须用 `inspecti`，不能用 `images -q <ref>` —— 后者**根本不过滤**：
      给它一个不存在的名字，它照样把节点上所有镜像的 ID 列出来。
      拿它当判据的话，只要节点上有任意镜像就会判成"生产镜像已就绪"，
      于是测试会声称自己跑的是生产镜像，实际跑的是 hostPath 拼装的那套。
      这种假阳性比直接失败坏得多：它让"镜像没验过"看起来像"验过了"。
    """
    import subprocess

    try:
        done = subprocess.run(
            ["crictl", "inspecti", "-q", PROD_IMAGE],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0

KUBECONFIG = Path("/etc/rancher/k3s/k3s.yaml")


def cluster_available() -> tuple[bool, str]:
    if not KUBECONFIG.exists():
        return False, "无可用 K8s 集群（需 k3s）"
    try:
        import kubernetes_asyncio  # noqa: F401, PLC0415
    except ImportError:
        return False, "缺 kubernetes-asyncio"
    if not VENV_SITE_PACKAGES.is_dir():
        return False, f"找不到 venv site-packages：{VENV_SITE_PACKAGES}"
    return True, ""


def model_endpoint():
    """从仓库根 .env 显式读模型端点配置。

    ★ 刻意不给 ClusterSettings 挂 env_file：那会让**所有**单测都继承开发机
      的配置，而这个坑这个项目已经踩过一次了（见 tests/conftest.py 里
      对象存储那条）。要对着真模型跑是一个 opt-in。
    """
    from atlas_cluster.config import ClusterSettings

    env = REPO_ROOT / ".env"
    if not env.exists():
        return None
    settings = ClusterSettings(_env_file=env)
    if not (settings.model_base_url and settings.model_api_key.get_secret_value()):
        return None
    return settings


def real_cli_available() -> tuple[bool, str]:
    """真 CLI 与模型端点都就位了吗。"""
    if model_endpoint() is None:
        return False, "未配置模型端点（.env 缺 ATLAS_CLUSTER_MODEL_BASE_URL / _API_KEY）"
    if prod_image_available():
        return True, ""
    if not (CLI_HOME / "node_modules/@zed-industries/claude-code-acp/dist/index.js").is_file():
        return False, (
            f"既没有生产镜像（{PROD_IMAGE}，用 make bridge-image 构建），"
            f"也没有 hostPath 回落所需的 claude-code-acp"
        )
    if not (NODE_HOME / "bin/node").is_file():
        return False, f"找不到 node：{NODE_HOME}"
    return True, ""


def inject_real_cli(manifest: dict[str, Any]) -> dict[str, Any]:
    """把 node 运行时与 CLI hostPath 挂进 bridge 容器。

    ★ 只补"CLI 怎么进容器"。模型端点、凭据、HOME 都由模板渲染 ——
      那正是要验的东西，测试不能替它注入。
    """
    spec = manifest["spec"]
    spec["volumes"] += [
        {"name": "node", "hostPath": {"path": str(NODE_HOME), "type": "Directory"}},
        {"name": "cli", "hostPath": {"path": str(CLI_HOME), "type": "Directory"}},
    ]
    bridge = spec["containers"][0]
    bridge["volumeMounts"] += [
        {"name": "node", "mountPath": str(NODE_HOME), "readOnly": True},
        {"name": "cli", "mountPath": str(CLI_HOME), "readOnly": True},
    ]
    # ★ node 需要 libatomic，而 python:3.14-slim 里没有。生产镜像同时装
    #   node 与 python，不存在这个问题；这里是把两个运行时硬拼在一起，
    #   所以要把缺的那个库跟 node 放在一起带进去。
    bridge["env"].append({"name": "LD_LIBRARY_PATH", "value": f"{NODE_HOME}/extra-lib"})
    return manifest


def inject_test_bridge(manifest: dict[str, Any], *, write_file: str = "") -> dict[str, Any]:
    """把源码与依赖 hostPath 挂进 bridge 容器，替代构建镜像。

    只加东西，不改模板已经渲染好的任何字段。
    """
    spec = manifest["spec"]
    spec["volumes"] += [
        {"name": "repo", "hostPath": {"path": str(REPO_ROOT), "type": "Directory"}},
        {
            "name": "site-packages",
            "hostPath": {"path": str(VENV_SITE_PACKAGES), "type": "Directory"},
        },
    ]
    bridge = spec["containers"][0]
    bridge["command"] = ["python", "-m", "atlas_bridge.main"]
    bridge["volumeMounts"] += [
        {"name": "repo", "mountPath": "/opt/atlas", "readOnly": True},
        {"name": "site-packages", "mountPath": "/opt/deps", "readOnly": True},
    ]
    bridge["env"] += [
        {"name": "PYTHONPATH", "value": "/opt/deps:/opt/atlas/bridge/src:/opt/atlas/acp/src"},
        # 根是只读的，写不了 .pyc
        {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
    ]
    if write_file:
        bridge["env"].append({"name": "ACP_FAKE_WRITE_FILE", "value": write_file})
    return manifest


def patch_manager_to_use_the_test_bridge(monkeypatch, *, write_file: str = "") -> None:
    """让 PodManager.ensure 渲染出来的 Pod 跑真 bridge。

    ★ 生命周期用例（调度、幂等、配额、回收）现在都要求 Pod 真的 Ready ——
      而 Ready 的含义已经收紧成"bridge 在监听"（readinessProbe）。
      拿一个起来就退出的占位镜像是过不了的，早先能过只是因为那时
      **没有任何一步等过 Pod 真的跑起来**。
    """
    from atlas_cluster import manager as manager_module

    real = manager_module.pod_manifest

    def _wrapped(req, settings):
        return inject_test_bridge(real(req, settings), write_file=write_file)

    monkeypatch.setattr(manager_module, "pod_manifest", _wrapped)
