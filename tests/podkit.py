"""对着真集群跑会话 Pod 的测试装配。

bridge 的生产镜像本机构建不了（开发机上没有任何镜像构建工具），所以这里
用 python:3.14-slim 加 hostPath 把仓库源码与 venv 挂进去，agent 用
atlas_bridge.testing.fake_agent（按剧本行事的 ACP agent）。

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

#: bridge 在容器里拉起的 agent（默认剧本：正常完成一轮）。
FAKE_ADAPTER_CMD = "python -m atlas_bridge.testing.fake_agent"

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


def inject_test_bridge(manifest: dict[str, Any]) -> dict[str, Any]:
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
    bridge["volumeMounts"] += [
        {"name": "repo", "mountPath": "/opt/atlas", "readOnly": True},
        {"name": "site-packages", "mountPath": "/opt/deps", "readOnly": True},
    ]
    bridge["env"] += [
        {
            "name": "PYTHONPATH",
            "value": "/opt/deps:/opt/atlas/bridge/src:/opt/atlas/acp/src"
            ":/opt/atlas/host/src:/opt/atlas/jsonrpc/src",
        },
        # 根是只读的，写不了 .pyc
        {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
    ]
    return manifest


def patch_manager_to_use_the_test_bridge(monkeypatch) -> None:
    """让 PodManager.ensure 渲染出来的 Pod 跑真 bridge。

    ★ 生命周期用例（调度、幂等、配额、回收）现在都要求 Pod 真的 Ready ——
      而 Ready 的含义已经收紧成"bridge 在监听"（readinessProbe）。
      拿一个起来就退出的占位镜像是过不了的，早先能过只是因为那时
      **没有任何一步等过 Pod 真的跑起来**。
    """
    from atlas_cluster import manager as manager_module

    real = manager_module.pod_manifest

    def _wrapped(req, settings):
        return inject_test_bridge(real(req, settings))

    monkeypatch.setattr(manager_module, "pod_manifest", _wrapped)
