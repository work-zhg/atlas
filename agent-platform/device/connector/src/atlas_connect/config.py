"""本机的路径和本机策略（c4-model/agent/connector/code-connector-session.html · LocalPolicy）。

本机策略写在 ~/.atlas/connect.toml 里，电脑主人改，平台改不了：允许的目录、权限模式上限、
同时几个会话、远程查看文件开不开、额外不给看的文件、每种 CLI 用什么命令启动。
"""

from __future__ import annotations

import fnmatch
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["MODE_ORDER", "CliCommand", "LocalPolicy", "Paths", "Rejection"]

#: CLI 的权限模式（ACP modeId）由宽到严。上限 = 允许的最宽的那个
MODE_ORDER = ("auto", "acceptEdits", "default", "plan")
#: 平台取值 → ACP modeId（启动参数里给的是平台取值）
PLATFORM_MODE = {"auto": "auto", "accept_edits": "acceptEdits", "manual": "default", "plan": "plan"}
MODE_NAMES = {"auto": "自动", "acceptEdits": "接受编辑", "default": "手动", "plan": "仅规划"}

DEFAULT_TOML = """\
# Atlas 连接器的本机策略。平台改不了这里；改完重启连接器生效（atlas-connect run）。

[policy]
# 项目目录只能在这些目录里（~ 是你的主目录）。
# 智能体的默认项目地址（通常是 ~/atlas-projects）也要在里面
allowed_roots = ["~/atlas-projects"]
# 权限模式上限：auto（自动）> acceptEdits（接受编辑）> default（手动）> plan（仅规划）
# 平台要求比这更宽的模式时，拒绝开会话
max_mode = "auto"
# 同时最多几个会话
max_sessions = 3
# 关掉后网页上看不到这台电脑上的文件
remote_files = true
# 网页上额外不给看的文件（以点开头的本来就不给看）
deny = ["*.pem", "*.key", "id_rsa*"]
# 会话多久没人用就停掉它的 bridge（秒）
idle_stop_s = 1800
# 除了基本的（PATH、HOME 等），还允许传给 CLI 的环境变量
env_allow = []

[cli.claude-code]
command = ["claude-agent-acp"]
install = "npm i -g @agentclientprotocol/claude-agent-acp"

[cli.codex]
command = ["codex-acp"]
install = "npm i -g @zed-industries/codex-acp"
"""

#: 传给 CLI 的基本环境变量（其余的按 env_allow 追加，模型接入另给）
BASE_ENV = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TZ",
    "TMPDIR",
    "SSH_AUTH_SOCK",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "NVM_DIR",
)


@dataclass(frozen=True)
class Paths:
    """连接器在电脑上的文件，都在 ~/.atlas 下（ATLAS_CONNECT_HOME 可改）。"""

    home: Path

    @classmethod
    def default(cls) -> Paths:
        raw = os.environ.get("ATLAS_CONNECT_HOME")
        return cls(Path(raw).expanduser() if raw else Path.home() / ".atlas")

    @property
    def config(self) -> Path:
        return self.home / "connect.toml"

    @property
    def device(self) -> Path:
        return self.home / "device.json"

    @property
    def audit(self) -> Path:
        return self.home / "audit.log"

    @property
    def run(self) -> Path:
        return self.home / "run"

    @property
    def socket(self) -> Path:
        return self.run / "connect.sock"

    def ensure(self) -> None:
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.run.mkdir(mode=0o700, exist_ok=True)


@dataclass(frozen=True)
class CliCommand:
    command: tuple[str, ...]
    install: str = ""


@dataclass(frozen=True)
class Rejection:
    """本机策略拒绝的原因，原路回到网页。"""

    reason: str
    message: str


@dataclass(frozen=True)
class LocalPolicy:
    allowed_roots: tuple[str, ...] = ("~/atlas-projects",)
    max_mode: str = "auto"
    max_sessions: int = 3
    remote_files: bool = True
    deny: tuple[str, ...] = ("*.pem", "*.key", "id_rsa*")
    idle_stop_s: float = 1800.0
    env_allow: tuple[str, ...] = ()
    clis: dict[str, CliCommand] = field(
        default_factory=lambda: {
            "claude-code": CliCommand(
                ("claude-agent-acp",), "npm i -g @agentclientprotocol/claude-agent-acp"
            ),
            "codex": CliCommand(("codex-acp",), "npm i -g @zed-industries/codex-acp"),
        }
    )

    @classmethod
    def load(cls, path: Path) -> LocalPolicy:
        """读配置；没有就写一份默认的。"""
        if not path.exists():
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_text(DEFAULT_TOML, encoding="utf-8")
        return cls.parse(tomllib.loads(path.read_text(encoding="utf-8")))

    @classmethod
    def parse(cls, data: dict[str, Any]) -> LocalPolicy:
        p = data.get("policy") or {}
        clis = {
            name: CliCommand(tuple(conf.get("command") or ()), str(conf.get("install") or ""))
            for name, conf in (data.get("cli") or {}).items()
            if conf.get("command")
        }
        max_mode = str(p.get("max_mode", "auto"))
        if max_mode not in MODE_ORDER:
            raise ValueError(f"max_mode 只能是 {' / '.join(MODE_ORDER)}，现在是 {max_mode!r}")
        return cls(
            allowed_roots=tuple(p.get("allowed_roots") or ()),
            max_mode=max_mode,
            max_sessions=int(p.get("max_sessions", 3)),
            remote_files=bool(p.get("remote_files", True)),
            deny=tuple(p.get("deny") or ()),
            idle_stop_s=float(p.get("idle_stop_s", 1800)),
            env_allow=tuple(p.get("env_allow") or ()),
            clis=clis,
        )

    # ------------------------------------------------------------------ 目录

    def roots(self) -> list[Path]:
        """允许的目录：展开 ~、解析符号链接后的真实路径。"""
        return [Path(r).expanduser().resolve() for r in self.allowed_roots]

    def within_roots(self, path: Path) -> bool:
        real = path.resolve()
        return any(real == root or real.is_relative_to(root) for root in self.roots())

    def denied(self, name: str) -> bool:
        return any(fnmatch.fnmatch(name, pattern) for pattern in self.deny)

    # ------------------------------------------------------------------ 权限模式

    def mode_allowed(self, mode_id: str | None) -> bool:
        """mode_id 是 ACP modeId。比上限更宽、或不认识的，都不允许。"""
        if mode_id is None:
            return True
        if mode_id not in MODE_ORDER:
            return False
        return MODE_ORDER.index(mode_id) >= MODE_ORDER.index(self.max_mode)

    def mode_rejection(self, mode_id: str | None) -> Rejection | None:
        if self.mode_allowed(mode_id):
            return None
        asked = MODE_NAMES.get(mode_id or "", mode_id)
        return Rejection(
            "mode_too_high",
            f"这个智能体要求「{asked}」执行，超过了这台电脑允许的上限「{MODE_NAMES[self.max_mode]}」。"
            "可以在电脑上改本机策略（~/.atlas/connect.toml 的 max_mode），"
            "或者请智能体的维护人调低操作权限。",
        )

    # ------------------------------------------------------------------ 开会话

    def check_open(self, launch: dict[str, Any], *, running: int) -> Rejection | None:
        """开会话前逐项核对：CLI 配过没有、权限模式、会话数。目录另由 ProjectDir 判。"""
        cli_type = str(launch.get("cli_type") or "")
        if cli_type not in self.clis:
            return Rejection(
                "cli_not_configured",
                f"这台电脑没有配置 {cli_type}：在 ~/.atlas/connect.toml 里加上 [cli.{cli_type}]，"
                "然后运行 atlas-connect doctor 检查",
            )
        mode = launch.get("mode")
        rejected = self.mode_rejection(
            PLATFORM_MODE.get(mode, mode) if isinstance(mode, str) else None
        )
        if rejected is not None:
            return rejected
        if running >= self.max_sessions:
            return Rejection(
                "too_many_sessions",
                f"这台电脑同时最多跑 {self.max_sessions} 个会话，现在已经满了。"
                "可以先结束一个（atlas-connect status / stop），或者改本机策略的 max_sessions",
            )
        return None

    def summary(self) -> dict[str, Any]:
        """报告给平台的本机策略（「我的设备」里只读显示）。"""
        return {
            "allowed_roots": list(self.allowed_roots),
            "max_mode": self.max_mode,
            "max_sessions": self.max_sessions,
            "remote_files": self.remote_files,
            "deny": list(self.deny),
        }
