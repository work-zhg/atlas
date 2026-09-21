"""真 Claude Code CLI 跑在会话 Pod 里 —— 模型走 Anthropic 兼容端点。

    cluster 建 Pod → 挂载器挂 GCS（工作区 + CLI 的 HOME）→ bridge 拉起
      真 claude-code-acp → 真模型答话 → 产物落进桶 → **第二轮 resume
      记得上一轮**

没有 k3s / 没配对象存储 / 没装 CLI / 没配模型端点，就整体跳过。

## 为什么必须用真 CLI 跑一遍

假 adapter 按剧本回话，它**不调模型、不写状态、不实现 resume**。
整个 acp 方案押注的那句话 —— "第二次委派时 CLI 记得上次改过什么" ——
在假 adapter 上是永远绿的，因为那里根本没有"记得"这回事。

第一次接真 CLI 就撞出一个模板缺口：**模板只注入 ATLAS_\\*，adapter 拿不到
任何模型凭据**，真 CLI 根本没法认证。假 adapter 不调模型，所以这个缺口
一路没被发现。

## 环境

    npm install --prefix /opt/acp-cli @zed-industries/claude-code-acp

    # .env（仓库根，gitignored）
    ATLAS_CLUSTER_MODEL_BASE_URL=https://api.deepseek.com/anthropic
    ATLAS_CLUSTER_MODEL_API_KEY=sk-...
    ATLAS_CLUSTER_MODEL_NAME=deepseek-chat

## 跑的是生产镜像

docker/bridge/Dockerfile 构建出来的镜像自带 node、adapter 与 bridge，
所以 Pod spec 里**一个 hostPath 都没有** —— 这正是"镜像构建也被覆盖到了"
的判据：还挂 hostPath 的话，测的就仍然是宿主上那份 node 与 CLI。

镜像没构建时回落到 hostPath 拼装（podkit.inject_real_cli），那条路能验
链路，但验不到镜像本身：依赖装没装对、入口点对不对、USER 是不是数字 UID，
全都绕过去了。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from . import podkit
from .conftest import REPO_ROOT

_NAMESPACE = "atlas-acp-cli"

#: 真模型一轮要几十秒，且 CLI 启动还要拉起 node —— 给宽一点。
_TURN_TIMEOUT_S = 240.0


def _object_storage():
    from atlas_server.config import Settings

    env = REPO_ROOT / ".env"
    if not env.exists():
        return None
    try:
        settings = Settings(_env_file=env)
    except Exception:  # noqa: BLE001
        return None
    if not (settings.workspace_configured and settings.oss_endpoint):
        return None
    return settings


def _available() -> tuple[bool, str]:
    ok, why = podkit.cluster_available()
    if not ok:
        return False, why
    ok, why = podkit.real_cli_available()
    if not ok:
        return False, why
    if _object_storage() is None:
        return False, "未配置真对象存储（.env 缺 OSS_*）"
    return True, ""


_OK, _WHY = _available()
pytestmark = pytest.mark.skipif(not _OK, reason=_WHY)


def _settings():
    from atlas_cluster.config import ClusterSettings

    oss = _object_storage()
    model = podkit.model_endpoint()
    return ClusterSettings(
        namespace=_NAMESPACE,
        workspace_mount="fuse",
        oss_bucket=oss.oss_bucket,
        oss_endpoint=oss.oss_endpoint,
        oss_region=oss.oss_region,
        oss_provider="GCS",
        oss_access_key_id=oss.oss_access_key_id.get_secret_value(),
        oss_secret_access_key=oss.oss_access_key_secret.get_secret_value(),
        # ★ 模型接入从 .env 来，但**经模板注入** —— 测试不替它塞环境变量
        model_base_url=model.model_base_url,
        model_api_key=model.model_api_key.get_secret_value(),
        model_name=model.model_name,
        # ★ 这里**不能**像别的用例那样把优雅终止压到 1s。挂载器要在那段
        #   窗口里把 VFS 缓存刷进对象存储；压短了就是 SIGKILL 掉一个还有
        #   脏数据的 rclone，表现是"Pod 重建后 CLI 失忆"——而那恰好是本组
        #   要验的东西，等于自己把被测行为关掉了。
        termination_grace_s=30,
        terminating_wait_s=60.0,
        pod_ready_timeout_s=180.0,
    )


#: 有生产镜像就用它 —— 那才是被部署的东西。没有则回落到 hostPath 拼装，
#: 能验链路但验不到镜像本身（依赖、入口点、数字 UID 全绕过去了）。
_USE_PROD_IMAGE = podkit.prod_image_available()


def _req(thread_id: str, user_id: str):
    from atlas_cluster.schemas import EnsurePodRequest

    return EnsurePodRequest(
        thread_id=thread_id,
        user_id=user_id,
        workspace_thread_id=thread_id,
        image=podkit.PROD_IMAGE if _USE_PROD_IMAGE else podkit.BRIDGE_IMAGE,
        adapter=podkit.PROD_ADAPTER_CMD if _USE_PROD_IMAGE else podkit.REAL_ADAPTER_CMD,
        # ★ 决定模板注入哪一组模型环境变量
        cli_type="claude-code",
    )


def _manifest(req, settings):
    """生产镜像自带一切 —— 一个 hostPath 都不挂。

    ★ 这正是"镜像构建被覆盖到了"的判据：Pod spec 里如果还有 hostPath，
      那测的就仍然是宿主上的那份 node 与 CLI，而不是镜像里的。
    """
    from atlas_cluster.template import pod_manifest

    manifest = pod_manifest(req, settings)
    if _USE_PROD_IMAGE:
        return manifest
    return podkit.inject_real_cli(podkit.inject_test_bridge(manifest))


@pytest.fixture
async def pod():
    """起一个跑真 CLI 的会话 Pod，用完连桶前缀一起清干净。"""
    import os

    os.environ["KUBECONFIG"] = str(podkit.KUBECONFIG)
    from atlas_cluster.k8s import KubernetesBackend
    from atlas_cluster.manager import PodManager
    from atlas_cluster.pairing import issue_token
    from atlas_cluster.template import secret_manifest
    from atlas_server.providers.filesystem import make_workspace

    await _ensure_namespace()
    backend = KubernetesBackend(_NAMESPACE, kubeconfig=str(podkit.KUBECONFIG))
    settings = _settings()
    manager = PodManager(backend, settings)
    thread_id = f"cli-{uuid.uuid4().hex[:8]}"
    user_id = "u-cli"
    token = issue_token()

    async def _launch() -> str:
        """建 Pod 并等就绪，返回 ws 地址。重复调用 = 重建（用于验 resume）。"""
        req = _req(thread_id, user_id)
        await backend.apply_secret(secret_manifest(req, token, settings))
        manifest = _manifest(req, settings)
        await backend.create_pod(manifest)
        pod_ref = await backend.wait_ready(
            manifest["metadata"]["name"], timeout_s=settings.pod_ready_timeout_s
        )
        return f"ws://{pod_ref.ip}:{settings.bridge_port}"

    async def _restart() -> str:
        """删掉 Pod 再起一个同会话的 —— CLI 的 HOME 在桶里，应当还在。"""
        await backend.delete_pod(f"atlas-sess-{thread_id}")
        await manager._await_terminating(thread_id)
        return await _launch()

    url = await _launch()
    try:
        yield url, token, thread_id, user_id, _restart
    finally:
        await manager.release(thread_id)
        workspace = make_workspace(_object_storage(), user_id, thread_id)
        if workspace is not None:
            workspace.delete("/skills/", recursive=True)
            workspace.delete("", recursive=True)
            _wipe_state(workspace, user_id, thread_id)
        await backend.aclose()


def _wipe_state(workspace, user_id: str, thread_id: str) -> None:
    """清掉 system/ 前缀 —— OssFilesystem 没有暴露它，直接走客户端。"""
    prefix = f"{user_id}/{thread_id}/system/"
    keys = [
        obj["Key"]
        for obj in workspace._s3.list_objects_v2(
            Bucket=workspace._bucket, Prefix=prefix
        ).get("Contents", [])
    ]
    for key in keys:
        workspace._s3.delete_object(Bucket=workspace._bucket, Key=key)


async def _ensure_namespace() -> None:
    from kubernetes_asyncio import client, config

    try:
        config.load_incluster_config()
    except Exception:  # noqa: BLE001
        await config.load_kube_config(str(podkit.KUBECONFIG))
    api = client.CoreV1Api()
    try:
        await api.create_namespace({"metadata": {"name": _NAMESPACE}})
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "status", None) != 409:
            raise
    finally:
        await api.api_client.close()


class Turn:
    """一轮的结果。"""

    def __init__(self, session_id: str, updates: list[dict]) -> None:
        self.session_id = session_id
        self.updates = updates

    @property
    def answer(self) -> str:
        return "".join(
            u.get("content", {}).get("text", "")
            for u in self.updates
            if u.get("sessionUpdate") == "agent_message_chunk"
        )

    @property
    def kinds(self) -> set[str]:
        return {u.get("sessionUpdate") for u in self.updates}


async def _turn(url: str, token: str, thread_id: str, prompt: str, *, session_id=None) -> Turn:
    """连上 Pod，跑一轮真模型对话。

    session_id 给了就走 session/load（恢复），否则 session/new。
    权限请求一律放行 —— 本组测的是链路与恢复，审批语义在
    test_acp_end_to_end.py 里验过。
    """
    from atlas_server.acp.channel import AcpChannel

    updates: list[dict] = []

    async def on_notification(frame: dict) -> None:
        if frame.get("method") == "session/update":
            updates.append(frame["params"]["update"])

    async def on_request(frame: dict):
        options = (frame.get("params") or {}).get("options") or []
        chosen = options[0]["optionId"] if options else "allow"
        return {"outcome": {"outcome": "selected", "optionId": chosen}}

    async with AcpChannel(
        url,
        token=token,
        thread_id=thread_id,
        on_notification=on_notification,
        on_request=on_request,
        connect_timeout_s=30.0,
    ) as channel:
        await channel.request(
            "initialize",
            {"protocolVersion": 1, "clientCapabilities": {"fs": {}}},
            timeout=60,
        )
        if session_id is None:
            created = await channel.request(
                "session/new", {"cwd": "/workspace", "mcpServers": []}, timeout=90
            )
            session_id = created["sessionId"]
        else:
            await channel.request(
                "session/load",
                {"sessionId": session_id, "cwd": "/workspace", "mcpServers": []},
                timeout=90,
            )
            updates.clear()  # load 会重放历史，别把它算进本轮

        await channel.request(
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": prompt}]},
            timeout=_TURN_TIMEOUT_S,
        )
    return Turn(session_id, updates)


# ──────────────────────────────────────────────── 真模型


@pytest.mark.skipif(not _USE_PROD_IMAGE, reason="未构建生产镜像（make bridge-image）")
def test_the_production_image_needs_no_host_mounts() -> None:
    """★ "镜像构建被覆盖到了" 的判据。

    生产镜像自带 node、adapter 与 bridge，所以渲染出来的 Pod spec 里
    **一个 hostPath 都不该有**。还挂着的话，跑的就仍然是宿主上那份
    node 与 CLI —— 依赖装没装对、入口点对不对、USER 是不是数字 UID，
    全都绕过去了，而测试看上去照样是绿的。
    """
    import json

    manifest = _manifest(_req("t-img", "u-img"), _settings())
    assert "hostPath" not in json.dumps(manifest)
    assert manifest["spec"]["containers"][0]["image"] == podkit.PROD_IMAGE


async def test_a_real_model_answers_through_the_pod(pod) -> None:
    """★ 端到端最基本的一条：模板注入的模型配置真的能驱动 CLI。

    早先模板只注入 ATLAS_*，adapter 一个凭据都拿不到 —— 真 CLI 在
    session/new 就会失败。假 adapter 不调模型，所以那个缺口一直没暴露。
    """
    url, token, thread_id, _user, _restart = pod

    turn = await _turn(url, token, thread_id, "只回答数字，不要解释：2 的 10 次方是多少？")

    assert "1024" in turn.answer, f"模型没给出答案：{turn.answer!r}"
    assert "agent_message_chunk" in turn.kinds


async def test_the_cli_writes_into_the_mounted_bucket(pod) -> None:
    """★ CLI 的工具真的作用在挂进来的 GCS 工作区上。

    不是"adapter 进程的 cwd 对"，是**模型调用写文件工具之后，对象出现在
    桶里** —— 中间隔着 CLI 的权限回路、文件工具、FUSE、对象存储。
    """
    from atlas_server.providers.filesystem import make_workspace

    url, token, thread_id, user_id, _restart = pod

    await _turn(
        url,
        token,
        thread_id,
        "在当前目录创建文件 hello.txt，内容就是一行：atlas-verified。创建完直接结束。",
    )

    workspace = make_workspace(_object_storage(), user_id, thread_id)
    content = await _eventually(lambda: workspace.read("hello.txt"))
    assert "atlas-verified" in content


# ──────────────────────────────────────────────── resume


async def test_the_cli_remembers_across_a_pod_restart(pod) -> None:
    """★ 整个 acp 方案押注的那句话：第二次委派时 CLI 记得上次干了什么。

    这里把 Pod **删掉重建**，恢复的依据只能是挂在对象存储上的 CLI HOME。
    HOME 落在 emptyDir 上的话这条必挂 —— 那正是加 /state 挂载之前的状态：
    resume 看起来能用，只要 Pod 一直活着；Pod 被回收之后就悄悄失忆了。

    ## ★ 这条曾经时灵时不灵，而原因值得记下来

    早先它只等"桶里出现了那个键"就去重建 Pod，于是三次里挂两次。
    真因是：**对象存在 ≠ 已经持久化**。rclone 会先把一个不完整的版本传上去
    （实测 139 字节），随后才用完整的覆盖（2124 字节）。撞在中间那段时间
    重建，新 Pod 拿到的就是个截断的会话记录，而 `--resume` 读它会直接让
    CLI 子进程退出 —— adapter 报的是 `Query closed before response received`，
    一句完全看不出"文件是半截的"的话。

    排查时我差点得出错误结论：在**另一次**运行里量到文件是完整的，就以为
    截断已被排除。跨运行拼观测在这种时灵时不灵的问题上是不成立的。

    所以现在等的是**大小稳定**，不是键存在。
    """
    url, token, thread_id, user_id, restart = pod

    first = await _turn(
        url, token, thread_id, "请记住这个暗号：菠萝三十七。记住就好，简短回复。"
    )
    assert first.session_id

    # ★ 先等会话记录**落到桶里**再拆 Pod。
    #
    #   不等的话测的就不是 resume，而是 rclone 的回写窗口：文件关闭到上传
    #   之间有一段（--vfs-write-back）数据只在容器本地，这期间杀 Pod 就丢。
    #   生产里这个窗口几乎碰不到 —— Pod 是闲置 idle_ttl_s（默认 30 分钟）
    #   之后才回收的。但它确实存在，所以单独记在这里：
    #   **突然被杀（OOM、节点故障）会丢掉最后一轮的记录**。
    await _eventually_stable(
        _object_storage(),
        f"{user_id}/{thread_id}/system/cli-home/",
        lambda key: first.session_id in key and key.endswith(".jsonl"),
    )

    # Pod 没了，容器本地的一切都没了 —— 只剩桶里的 HOME
    new_url = await restart()

    second = await _turn(
        new_url,
        token,
        thread_id,
        "我刚才让你记的暗号是什么？只回答暗号本身。",
        session_id=first.session_id,
    )
    assert "菠萝" in second.answer and "三十七" in second.answer, (
        f"Pod 重建后 CLI 不记得上一轮了：{second.answer!r}"
    )


async def test_the_state_directory_actually_lands_in_object_storage(pod) -> None:
    """把上一条的**依据**单独钉住。

    上一条挂了只知道"不记得了"；这条直接说明是 HOME 没落进桶 —— 两种
    失败的排查方向完全不同。
    """
    url, token, thread_id, user_id, _restart = pod
    oss = _object_storage()

    await _turn(url, token, thread_id, "简短回复：你好。")

    prefix = f"{user_id}/{thread_id}/system/cli-home/"
    # CLI 把会话记录写成 ~/.claude/projects/<cwd-slug>/<sessionId>.jsonl，
    # 而 cwd 是 /workspace ⇒ slug 恒为 -workspace，父子会话不会撞。
    def _is_transcript(key: str) -> bool:
        return ".claude/projects/" in key and key.endswith(".jsonl")

    keys = await _eventually_keys(oss, prefix, _is_transcript)
    assert any(_is_transcript(key) for key in keys), f"桶里没有 CLI 的会话记录，只有：{keys[:10]}"


# ──────────────────────────────────────────────── 辅助


async def _eventually(read, *, timeout_s: float = 60.0) -> str:
    """等对象在桶里出现（rclone 的写经 VFS 缓存异步刷出去）。"""
    deadline = asyncio.get_running_loop().time() + timeout_s
    last = "（从未读到）"
    while asyncio.get_running_loop().time() < deadline:
        result = read()
        if result.error is None:
            return result.file_data["content"]
        last = result.error
        await asyncio.sleep(1.0)
    pytest.fail(f"对象始终没出现在桶里：{last}")


async def _eventually_stable(oss, prefix: str, match, *, timeout_s: float = 120.0) -> int:
    """等对象出现**并且大小不再变化**。

    ★ "对象已存在"不等于"已经持久化"。实测里会话记录会先以一个不完整的
      大小出现（139 字节），随后才被完整覆盖（2124 字节）—— 中间这段时间
      里去重建 Pod，新 Pod 拿到的就是个截断的记录，而 --resume 读它会失败。

      这正是这条用例一开始时灵时不灵的原因：早先只等"键存在"，于是赌的是
      两次上传之间的时间差。
    """
    from atlas_server.providers.filesystem import make_workspace

    workspace = make_workspace(oss, "probe", "probe")
    deadline = asyncio.get_running_loop().time() + timeout_s
    last_size = -1
    stable = 0
    while asyncio.get_running_loop().time() < deadline:
        sizes = [
            obj["Size"]
            for obj in workspace._s3.list_objects_v2(
                Bucket=workspace._bucket, Prefix=prefix
            ).get("Contents", [])
            if match(obj["Key"])
        ]
        size = sizes[0] if sizes else -1
        if size > 0 and size == last_size:
            stable += 1
            if stable >= 3:  # 连续三次不变 ⇒ 上传已经结束
                return size
        else:
            stable = 0
        last_size = size
        await asyncio.sleep(2.0)
    pytest.fail(f"会话记录始终没稳定下来（最后一次读到 {last_size} 字节）")


async def _eventually_keys(oss, prefix: str, match, *, timeout_s: float = 90.0) -> list[str]:
    """等到**想要的那个**对象出现。

    ★ 不能"列到任何对象就返回"：CLI 往 HOME 下写很多东西（todos、debug、
      .claude.json 备份），它们先落、会话记录后落。早返回的话断言看到的是
      一堆无关文件，报出来的却像是"会话记录没写"。
    """
    from atlas_server.providers.filesystem import make_workspace

    workspace = make_workspace(oss, "probe", "probe")
    deadline = asyncio.get_running_loop().time() + timeout_s
    keys: list[str] = []
    while asyncio.get_running_loop().time() < deadline:
        keys = [
            obj["Key"]
            for obj in workspace._s3.list_objects_v2(
                Bucket=workspace._bucket, Prefix=prefix
            ).get("Contents", [])
        ]
        if any(match(key) for key in keys):
            return keys
        await asyncio.sleep(2.0)
    return keys
