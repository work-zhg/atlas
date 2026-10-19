"""一个沙箱 Pod：K8s 按 cluster 渲染的 manifest 把它跑起来（内存模拟，§5）。

真实的 Pod 有五个容器：

    sandboxd-installer  init：把自带解释器的 sandboxd 拷进共享盘
                        —— 所以沙箱镜像可以是任意 glibc 镜像
    oss-workspace       原生 sidecar：rclone mount → /workspace（rw），远程控制带口令
    oss-skills          原生 sidecar：rclone mount → /skills（ro）                    （demo 省略）
    barrier             原生 sidecar：持有同一个口令，只暴露 forget / queue
    sandbox             主容器：进程 = sandboxd；环境里没有任何凭据；非 root、根只读（demo 做不到）

demo 只保留和执行有关的三个：挂载器、barrier、sandboxd。它们的目录都在一个临时目录下：

    workspace/   挂载点，命令的 cwd       Pod 没了它也没了；内容在对象存储里，下一个 Pod 挂上来还在
    home/        HOME（emptyDir）         Pod 没了就没了：装进 HOME 的依赖、缓存都会丢
    tmp/         /tmp（emptyDir）         sandboxd 的完整输出日志也在这里
    secrets/     Secret 卷                令牌文件
"""

from __future__ import annotations

import secrets
import shutil
from pathlib import Path
from typing import Any

from sandboxd import ExecManager, LinkFaults, serve
from storage import Barrier, FuseMount, ObjectStorage, ensure_dir


class SandboxPod:
    def __init__(self, manifest: dict[str, Any], base: Path) -> None:
        self.manifest = manifest
        self.name: str = manifest["name"]
        self.labels: dict[str, str] = manifest["labels"]
        self.base = base
        self.ready = False
        self.url = ""
        self.manager: ExecManager | None = None

    def start(self, token: str, storage: ObjectStorage, faults: LinkFaults) -> None:
        """kubelet 起容器：Secret 卷 → 挂载器 → barrier → 主容器。"""
        home, tmp = ensure_dir(self.base / "home"), ensure_dir(self.base / "tmp")
        token_file = ensure_dir(self.base / "secrets") / "token"
        token_file.write_text(token)
        # rc 口令：真实的经 secretKeyRef 只注入挂载器和 barrier 两个容器，主容器里没有
        rc_pass = secrets.token_hex(8)
        self.mount = FuseMount(
            storage, self.manifest["workspace_prefix"], ensure_dir(self.base / "workspace"), rc_pass
        )
        barrier = Barrier(self.mount, rc_pass)
        # 主容器的环境 = 模板给的那几个 + 镜像自带的 PATH / LANG。demo 把 /home/agent 换成临时目录
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "en_US.UTF-8",
            "TMPDIR": str(tmp),
            **self.manifest["env"],
            "HOME": str(home),
            "SANDBOXD_TOKEN_FILE": str(token_file),
        }
        self.manager = ExecManager(
            env=env,
            workspace=self.mount.local,
            log_dir=ensure_dir(tmp / ".sandboxd"),
            barrier=barrier,
        )
        self._http = serve(
            self.manager,
            token_file=token_file,
            session_id=self.manifest["env"]["SANDBOXD_SESSION_ID"],
            faults=faults,
        )
        self.url = f"http://127.0.0.1:{self._http.server_address[1]}"  # 「Pod IP」
        self.ready = True

    @property
    def boot_id(self) -> str:
        return self.manager.boot_id if self.manager else "-"

    def delete(self) -> None:
        """容器全停：进程全没、端口关闭、emptyDir 清掉。对象存储上的工作区不受影响。"""
        self.ready = False
        if self.manager is not None:
            self.manager.shutdown()
            self._http.shutdown()
            self._http.server_close()
            self.mount.unmount()
        shutil.rmtree(self.base, ignore_errors=True)
