"""OpenSandbox（内存模拟）：两个 lifecycle server 实例、一个 BatchSandbox 控制器、Pod、egress。

    opensandbox-sbx ─┐  POST /v1/sandboxes：请求 + 本实例的模板 → BatchSandbox CR
    opensandbox-acp ─┴─▶ Controller：CR → Pod（execd 注入、PVC subPath 挂载、模板里的卷）
                          Pod：execd :44772；ACP 镜像另有 bridge :8900；
                          带了网络策略的有 egress sidecar

★ 模板合并照搬上游（server/opensandbox_server/services/k8s/template_manager._deep_merge、
  batchsandbox_provider._merge_pod_spec_extras）：遇到列表是运行时值整体覆盖，事后只补回模板里的
  volumes、主容器的 volumeMounts 与 securityContext —— 所以模板里加不了 sidecar
  （设计 §4 的根因，实验 5）。
★ 模板是 server 级全局的：ACP 要挂模型 key、原生沙箱绝不能有，所以按 kind 部署两个实例（设计 §7）。
★ Network 是「集群网络」：所有 HTTP 都经过它（httpx.MockTransport），按 host 分发；
  Pod 没了就 ConnectError；剧本可以注入一次「连接被重置」。
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import shutil
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from execd import PORT as EXECD_PORT
from execd import Execd
from images import BRIDGE_PORT, FakeBridge
from storage import CsiMount, ObjectStorage
from tracing import say

EXECD_IMAGE = "opensandbox/execd:v1.1.0"
EGRESS_IMAGE = "opensandbox/egress:v1.1.7"
STARTUP_S = 0.2  # 冷启动（本机实测约 5 秒）
API_KEYS = {"opensandbox-sbx": "sbx-key", "opensandbox-acp": "acp-key"}

Handler = Callable[[httpx.Request], Awaitable[httpx.Response]]

# ─────────────────────────────────────────────────────────────── 模板（运维配置，server 级）

_STRICT = {
    "runAsNonRoot": True, "runAsUser": 1000, "readOnlyRootFilesystem": True,
    "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]},
}  # fmt: skip


def _empty(name: str, path: str) -> tuple[dict, dict]:
    return {"name": name, "emptyDir": {}}, {"name": name, "mountPath": path}


def _template(volumes: list[tuple[dict, dict]], security: dict) -> dict[str, Any]:
    return {
        "spec": {
            "template": {
                "spec": {
                    "terminationGracePeriodSeconds": 30,
                    "volumes": [v for v, _ in volumes],
                    "containers": [
                        {
                            "name": "sandbox",
                            "volumeMounts": [m for _, m in volumes],
                            "securityContext": security,
                        }
                    ],
                }
            }
        }
    }


SBX_TEMPLATE = _template(
    [_empty("tmp", "/tmp"), _empty("home", "/home/agent"), _empty("repos", "/repos")], _STRICT
)
ACP_TEMPLATE = _template(
    [
        _empty("tmp", "/tmp"),
        _empty("repos", "/repos"),
        # ★ 模型 key：静态 Secret 以文件挂进 ACP 的 Pod。只在这个实例的模板里，原生沙箱拿不到
        (
            {"name": "model-key", "secret": {"secretName": "atlas-acp-model"}},
            {"name": "model-key", "mountPath": "/run/secrets/atlas", "readOnly": True},
        ),
    ],
    {"runAsNonRoot": True, "runAsUser": 1000},
)
K8S_SECRETS = {"atlas-acp-model": {"model_api_key": "sk-demo-model-key"}}  # 运维建的 Secret


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """上游的 _deep_merge：dict 递归合并，其余（★ 包括 list）运行时值整体覆盖。"""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if value is None:
            continue
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def merge_pod_spec_extras(cr: dict[str, Any], template: dict[str, Any]) -> None:
    """上游的 _merge_pod_spec_extras：只把模板的 volumes、主容器的挂载与 securityContext 补回来。"""
    tpl = template.get("spec", {}).get("template", {}).get("spec", {})
    spec = cr["spec"]["template"]["spec"]
    names = {v["name"] for v in spec["volumes"]}
    spec["volumes"] += [v for v in tpl.get("volumes", []) if v["name"] not in names]
    tpl_main = (tpl.get("containers") or [{}])[0]
    main = spec["containers"][0]
    main["securityContext"] = {
        **tpl_main.get("securityContext", {}),
        **main.get("securityContext", {}),
    }
    names = {m["name"] for m in main["volumeMounts"]}
    main["volumeMounts"] += [m for m in tpl_main.get("volumeMounts", []) if m["name"] not in names]


# ─────────────────────────────────────────────────────────────── 集群网络


@dataclass
class Fault:
    """一次性故障：条件成立后，下一个发往 Pod 的请求（或正在读的流）被重置。"""

    ready: Callable[[], bool]
    label: str
    fired: bool = False


class Network:
    def __init__(self) -> None:
        self.hosts: dict[str, Handler] = {}
        self.faults: list[Fault] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._dispatch)

    def cut_once(self, ready: Callable[[], bool], label: str) -> None:
        self.faults.append(Fault(ready, label))

    async def _dispatch(self, request: httpx.Request) -> httpx.Response:
        to_pod = request.url.host.startswith("10.")
        if to_pod:
            self._maybe_cut(request)
        handler = self.hosts.get(request.url.host)
        if handler is None:
            raise httpx.ConnectError(
                f"连不上 {request.url.host}（Pod 已经不在了）", request=request
            )
        response = await handler(request)
        if to_pod and response.headers.get("content-type") == "text/event-stream":
            response.stream = _Watched(response.stream, self, request)  # 流读到一半也可能被断
        return response

    def _maybe_cut(self, request: httpx.Request) -> None:
        for fault in self.faults:
            if not fault.fired and fault.ready():
                fault.fired = True
                say("网络", f"剧本注入：{fault.label} → 这个请求的连接被重置")
                raise httpx.ReadError("Connection reset by peer", request=request)


class _Watched(httpx.AsyncByteStream):
    def __init__(self, inner: Any, net: Network, request: httpx.Request) -> None:
        self._inner, self._net, self._request = inner, net, request

    async def __aiter__(self):
        async for chunk in self._inner:
            self._net._maybe_cut(self._request)
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


# ─────────────────────────────────────────────────────────────── Pod


class Egress:
    """egress sidecar：和沙箱共享网络命名空间，出站流量都经过它。这里只模拟 Credential Vault。"""

    def __init__(self, pod: str, network: Network) -> None:
        self.pod, self._network = pod, network
        self.vault: dict[str, Any] = {"credentials": {}, "bindings": []}

    def configure(self, body: dict[str, Any]) -> None:
        self.vault["credentials"].update({c["name"]: c for c in body["credentials"]})
        self.vault["bindings"] += body["bindings"]

    async def request(self, method: str, url: str) -> httpx.Response:
        """沙箱里的进程发出的请求（真实是透明拦截：dns+nft 把流量导进 sidecar 的 MITM）。"""
        host = urllib.parse.urlparse(url).hostname
        headers = {}
        for binding in self.vault["bindings"]:
            if binding["host"] == host:
                cred = self.vault["credentials"][binding["credential"]]
                raw = f"{cred['username']}:{cred['secret']}".encode()
                headers["authorization"] = "Basic " + base64.b64encode(raw).decode()
                say(
                    "egress",
                    f"{self.pod} 出站 {host} 命中 Credential Vault 绑定 → 注入认证头"
                    "（沙箱里看不到明文）",
                )
        async with httpx.AsyncClient(transport=self._network.transport()) as client:
            return await client.request(method, url, headers=headers)


class Pod:
    def __init__(
        self, cr: dict[str, Any], ip: str, base: Path, storage: ObjectStorage, network: Network
    ) -> None:
        self.cr, self.ip, self.base = cr, ip, base
        self.name: str = cr["metadata"]["name"]
        self._storage, self._network = storage, network
        self.paths: dict[str, Path] = {}  # 容器路径 → 本机目录
        self.mounts: list[CsiMount] = []
        self.execd: Execd | None = None
        self.bridge: FakeBridge | None = None
        self.egress: Egress | None = None

    @property
    def spec(self) -> dict[str, Any]:
        return self.cr["spec"]["template"]["spec"]

    async def start(self) -> None:
        """kubelet：先建卷（CSI 挂载在容器启动前就绪，所以 subPath 能用），再起容器。"""
        main = self.spec["containers"][0]
        volumes = {v["name"]: v for v in self.spec["volumes"]}
        for m in main["volumeMounts"]:
            vol, local = volumes[m["name"]], self.base / m["mountPath"].strip("/").replace("/", "_")
            if "persistentVolumeClaim" in vol:  # 整桶 PVC：只看得到 subPath
                mount = CsiMount(
                    self._storage,
                    m["subPath"] + "/",
                    local,
                    owner=self.name,
                    read_only=m.get("readOnly", False),
                )
                await mount.mount()
                self.mounts.append(mount)
            elif "secret" in vol:
                local.mkdir(parents=True)
                for key, value in K8S_SECRETS[vol["secret"]["secretName"]].items():
                    (local / key).write_text(value)
            else:
                local.mkdir(parents=True, exist_ok=True)
            self.paths[m["mountPath"]] = local
        env = {e["name"]: self._local_env(e["value"]) for e in main.get("env", [])}
        env |= {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "en_US.UTF-8"}
        if any(c["name"] == "egress" for c in self.spec["containers"]):
            self.egress = Egress(self.name, self._network)
        self.execd = Execd(
            pod=self.name,
            token=env.get("EXECD_ACCESS_TOKEN", ""),
            resolve=self.resolve,
            env=env,
            on_close=self.flush,
        )
        if main["image"].startswith("atlas-acp"):
            self.bridge = FakeBridge(self.name, env, self.resolve, self.egress, on_close=self.flush)

    def resolve(self, path: str) -> Path:
        """容器路径 → 本机目录。只认挂载过的目录（真实容器里能看到整个文件系统）。"""
        for mount_path in sorted(self.paths, key=len, reverse=True):
            if path == mount_path or path.startswith(mount_path + "/"):
                return self.paths[mount_path] / path[len(mount_path) :].lstrip("/")
        raise FileNotFoundError(path)

    def flush(self) -> None:
        for mount in self.mounts:
            mount.flush()

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.port == EXECD_PORT and self.execd:
            return await self.execd.handle(request)
        if request.url.port == BRIDGE_PORT and self.bridge:
            return await self.bridge.handle(request)
        raise httpx.ConnectError("端口没有在监听", request=request)

    async def stop(self) -> None:
        if self.execd:
            self.execd.kill_all()
        for mount in self.mounts:
            await mount.unmount()
        shutil.rmtree(self.base, ignore_errors=True)  # emptyDir 随 Pod 消失

    def describe(self) -> str:
        spec = self.spec
        init = "、".join(c["name"] for c in spec["initContainers"])
        containers = "、".join(c["name"] for c in spec["containers"])
        mounts = "、".join(m["mountPath"] for m in spec["containers"][0]["volumeMounts"])
        return f"init [{init}]  容器 [{containers}]  主容器挂载 [{mounts}]"

    def _local_env(self, value: str) -> str:
        for mount_path in ("/home/agent", "/tmp", "/state"):
            if value == mount_path:
                return str(self.base / mount_path.strip("/").replace("/", "_"))
        return value


# ─────────────────────────────────────────────────────────────── 控制器


class Controller:
    """BatchSandbox 控制器：一个，管两个命名空间。restartPolicy 默认 Never：容器崩了 → Failed。"""

    def __init__(self, network: Network, storage: ObjectStorage, base: Path) -> None:
        self._network, self._storage, self._base = network, storage, base
        self.crs: dict[str, dict[str, Any]] = {}
        self.state: dict[str, str] = {}
        self.pods: dict[str, Pod] = {}
        self.created = 0

    def apply(self, cr: dict[str, Any]) -> None:
        sid = cr["metadata"]["name"]
        self.crs[sid], self.state[sid] = cr, "Pending"
        cr["metadata"]["createdAt"] = time.monotonic()
        asyncio.get_running_loop().create_task(self._run(sid))

    async def _run(self, sid: str) -> None:
        await asyncio.sleep(STARTUP_S)
        if sid not in self.crs:
            return  # 还没起来就被删了
        self.created += 1
        pod = Pod(
            self.crs[sid], f"10.0.0.{self.created}", self._base / sid, self._storage, self._network
        )
        await pod.start()
        self.pods[sid] = pod
        self._network.hosts[pod.ip] = pod.handle
        self.state[sid] = "Running"
        say("控制器", f"{sid} 的 Pod 就绪（{pod.ip}，冷启动 {STARTUP_S:g} 秒）：{pod.describe()}")

    async def wait_running(self, sid: str, timeout: float = 5) -> None:
        deadline = time.monotonic() + timeout
        while self.state.get(sid) == "Pending" and time.monotonic() < deadline:
            await asyncio.sleep(0.01)

    async def delete(self, sid: str) -> None:
        pod = self.pods.pop(sid, None)
        if pod:
            self._network.hosts.pop(pod.ip, None)
            await pod.stop()
        self.crs.pop(sid, None)
        self.state.pop(sid, None)

    async def fail(self, sid: str, reason: str) -> None:
        """故障注入：节点故障 / OOM。Pod 没了、CR 还在，状态 Failed（不会原地重启）。"""
        pod = self.pods.pop(sid)
        self._network.hosts.pop(pod.ip, None)
        await pod.stop()
        self.state[sid] = "Failed"
        say("控制器", f"{sid} 的 Pod 消失（{reason}）→ Failed（restartPolicy: Never，不原地重启）")


# ─────────────────────────────────────────────────────────────── lifecycle server


class LifecycleServer:
    def __init__(
        self, name: str, namespace: str, template: dict[str, Any], controller: Controller
    ) -> None:
        self.name, self.namespace, self.template = name, namespace, template
        self._controller = controller
        self._n = 0

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("open-sandbox-api-key") != API_KEYS[self.name]:
            return httpx.Response(401)
        parts = request.url.path.removeprefix("/v1/sandboxes").strip("/").split("/")
        sid = parts[0]
        if request.method == "POST" and not sid:
            return await self._create(json.loads(request.content))
        if request.method == "GET" and not sid:
            raw = urllib.parse.unquote(request.url.params.get("metadata", ""))
            want = dict(urllib.parse.parse_qsl(raw))
            items = [self._view(s) for s in self._mine() if want.items() <= self._labels(s).items()]
            return httpx.Response(200, json={"items": items})
        if sid not in self._mine():
            return httpx.Response(404)
        if request.method == "GET" and len(parts) == 1:
            return httpx.Response(200, json=self._view(sid))
        if request.method == "DELETE":
            say("lifecycle", f"{self.name} 删除 {sid}")
            await self._controller.delete(sid)
            return httpx.Response(204)
        if parts[1:2] == ["endpoints"]:
            pod = self._controller.pods.get(sid)
            if pod is None:
                return httpx.Response(404)
            return httpx.Response(200, json={"endpoint": f"{pod.ip}:{parts[2]}"})  # 直连 Pod IP
        if parts[1:2] == ["credential-vault"]:
            pod = self._controller.pods[sid]
            assert pod.egress is not None, (
                "没有 egress sidecar（创建时没带 networkPolicy / credentialProxy）"
            )
            pod.egress.configure(json.loads(request.content))
            return httpx.Response(200)
        return httpx.Response(404)

    async def _create(self, body: dict[str, Any]) -> httpx.Response:
        self._n += 1
        sid = f"{self.namespace.removeprefix('atlas-')}-{self._n}"
        cr = self._build_cr(sid, body)
        meta = body["metadata"]
        say(
            "lifecycle",
            f"{self.name} 创建 {sid}（{meta['atlas.kind']} / 会话 {meta['atlas.thread']}）→ 控制器",
        )
        self._controller.apply(cr)
        await self._controller.wait_running(sid)  # 等到 Running 才返回
        if sid not in self._controller.crs:
            return httpx.Response(409, json={"code": "DELETED_DURING_CREATE", "id": sid})
        return httpx.Response(200, json=self._view(sid))

    def _build_cr(self, sid: str, body: dict[str, Any]) -> dict[str, Any]:
        """运行时清单（server 生成）+ 模板 → BatchSandbox CR。"""
        main = {
            "name": "sandbox",
            "image": body["image"]["uri"],
            "command": ["/opt/opensandbox/bootstrap.sh", *body["entrypoint"]],
            "env": [{"name": k, "value": v} for k, v in body.get("env", {}).items()],
            "volumeMounts": [{"name": "opensandbox-bin", "mountPath": "/opt/opensandbox"}],
        }
        spec: dict[str, Any] = {
            "initContainers": [
                {"name": "execd-installer", "image": EXECD_IMAGE}
            ],  # 所有沙箱都注入 execd
            "containers": [main],
            "volumes": [{"name": "opensandbox-bin", "emptyDir": {}}],
            "restartPolicy": "Never",
        }
        if body.get("networkPolicy") or body.get("credentialProxy", {}).get("enabled"):
            spec["containers"].append({"name": "egress", "image": EGRESS_IMAGE})
        for vol in body.get("volumes", []):  # volume_helper.apply_volumes_to_pod_spec
            claim = vol["pvc"]["claimName"]
            if claim not in {v["name"] for v in spec["volumes"]}:
                spec["volumes"].append(
                    {"name": claim, "persistentVolumeClaim": {"claimName": claim}}
                )
            main["volumeMounts"].append(
                {
                    "name": claim,
                    "mountPath": vol["mountPath"],
                    "subPath": vol["subPath"],
                    "readOnly": vol.get("readOnly", False),
                }
            )
        labels = {**body["metadata"], "opensandbox.io/id": sid}
        runtime = {
            "metadata": {"name": sid, "namespace": self.namespace, "labels": labels},
            "spec": {"replicas": 1, "template": {"metadata": {"labels": labels}, "spec": spec}},
        }
        cr = deep_merge(self.template, runtime)  # ★ 列表整体覆盖
        merge_pod_spec_extras(cr, self.template)
        return cr

    def _mine(self) -> list[str]:
        return [
            s
            for s, cr in self._controller.crs.items()
            if cr["metadata"]["namespace"] == self.namespace
        ]

    def _labels(self, sid: str) -> dict[str, str]:
        return self._controller.crs[sid]["metadata"]["labels"]

    def _view(self, sid: str) -> dict[str, Any]:
        meta = self._controller.crs[sid]["metadata"]
        return {
            "id": sid,
            "status": {"state": self._controller.state[sid]},
            "metadata": meta["labels"],
            "createdAt": meta["createdAt"],
        }
