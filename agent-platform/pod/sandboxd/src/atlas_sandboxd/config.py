"""配置：全部来自环境变量（cluster 的 Pod 模板注入）。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

__all__ = ["BarrierConfig", "ServeConfig"]


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    return int(raw) if raw.strip() else default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    return float(raw) if raw.strip() else default


@dataclass
class ServeConfig:
    port: int = 8910
    host: str = "0.0.0.0"
    #: per-Pod 令牌文件（Secret 挂载）。为空 = 不鉴权，只给单测用
    token_file: str = ""
    #: 会话绑定：请求头 X-Atlas-Sandbox 必须等于它（同 bridge 的会话绑定）
    session_id: str = ""
    workspace: str = "/workspace"
    #: 就绪前要求 workspace 已是挂载点（fuse 模式）
    require_mount: bool = False
    #: 屏障 sidecar 的地址；空 = 没有屏障（none 模式 / 单测）
    barrier_url: str = ""
    max_concurrency: int = 8
    #: 返回给模型的输出上限（头尾各一半）；完整输出在日志文件里
    output_limit_bytes: int = 100_000
    #: 内存里为「重新接上」保留的输出事件总量
    replay_limit_bytes: int = 1_000_000
    #: 结束后保留记录多久（供断线重连取结果）
    retention_s: float = 3600.0
    #: 静默卡死判定的默认值（请求可覆盖；0 = 不判定）
    idle_s: float = 600.0
    #: kill 时 SIGTERM 到 SIGKILL 的宽限
    kill_grace_s: float = 5.0
    heartbeat_s: float = 15.0
    log_dir: str = "/tmp/.sandboxd"
    #: 不传给命令的环境变量前缀：守护进程自己的配置。
    #: ★ 其余照传 —— 镜像自己定义的变量（JAVA_HOME、NODE_PATH、PIP_*…）命令要用；
    #:   主容器的环境里本就没有任何凭据（cluster 模板保证，test_cluster_sandbox 守着）。
    env_deny_prefixes: tuple[str, ...] = ("SANDBOXD_",)

    @classmethod
    def from_env(cls) -> ServeConfig:
        d = cls()
        return cls(
            port=_int("SANDBOXD_PORT", d.port),
            host=os.environ.get("SANDBOXD_HOST", d.host),
            token_file=os.environ.get("SANDBOXD_TOKEN_FILE", ""),
            session_id=os.environ.get("SANDBOXD_SESSION_ID", ""),
            workspace=os.environ.get("SANDBOXD_WORKSPACE", d.workspace),
            require_mount=os.environ.get("SANDBOXD_REQUIRE_MOUNT", "") == "1",
            barrier_url=os.environ.get("SANDBOXD_BARRIER_URL", ""),
            max_concurrency=_int("SANDBOXD_MAX_CONCURRENCY", d.max_concurrency),
            output_limit_bytes=_int("SANDBOXD_OUTPUT_LIMIT_BYTES", d.output_limit_bytes),
            retention_s=_float("SANDBOXD_RETENTION_S", d.retention_s),
            idle_s=_float("SANDBOXD_IDLE_S", d.idle_s),
            log_dir=os.environ.get("SANDBOXD_LOG_DIR", d.log_dir),
        )

    def token(self) -> str:
        return Path(self.token_file).read_text().strip() if self.token_file else ""


@dataclass
class BarrierConfig:
    port: int = 5573
    #: rclone 远程控制（挂载器 sidecar）
    rc_url: str = "http://127.0.0.1:5572"
    rc_user: str = "sbx"
    rc_pass: str = ""

    @classmethod
    def from_env(cls) -> BarrierConfig:
        d = cls()
        return cls(
            port=_int("BARRIER_PORT", d.port),
            rc_url=os.environ.get("BARRIER_RC_URL", d.rc_url),
            rc_user=os.environ.get("RCLONE_RC_USER", d.rc_user),
            rc_pass=os.environ.get("RCLONE_RC_PASS", ""),
        )
