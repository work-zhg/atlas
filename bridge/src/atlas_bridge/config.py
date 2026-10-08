"""BridgeConfig：来自环境变量 ATLAS_BRIDGE_*（代码设计 §10）。

这里只有实现参数与部署参数。策略数值（每轮的截止、静默、等待、重连窗口）不在这里 ——
它们随 session.open 与 turn.start 下发；``fallback_limits`` 只在 server 没给时使用（B5）。
凭据以文件形式给出（token_file），不放进环境变量：环境变量会被子进程继承（§8.2）。
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["BridgeConfig"]

PREFIX = "ATLAS_BRIDGE_"
#: 这些变量设为空串表示「不设置」
_OPTIONAL = frozenset({"agent_uid", "agent_gid", "agent_oom_score_adj"})


class BridgeConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    #: 本 Pod 服务的会话（握手头 X-Atlas-Session 必须等于它，§4.3）
    session_id: str = Field(min_length=1)
    token_file: Path = Path("/run/secrets/bridge-token")
    listen_host: str = "0.0.0.0"
    listen_port: int = 8900

    #: 适配器命令
    agent_cmd: tuple[str, ...] = Field(min_length=1)
    #: None = 不切换用户（本机开发）；容器里必须设置（§8.3）
    agent_uid: int | None = None
    agent_gid: int | None = None
    #: 允许传给 agent 的环境变量名（白名单，§8.3）
    agent_env_allow: tuple[str, ...] = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TZ")
    #: 写 /proc/<pid>/oom_score_adj；None 跳过（非 Linux）
    agent_oom_score_adj: int | None = 900
    workspace: Path = Path("/workspace")

    # §6.1 的实现参数（非策略，B5）
    boot_timeout_s: float = 60
    open_timeout_s: float = 120
    acp_request_timeout_s: float = 30
    cancel_grace_s: float = 15
    kill_grace_s: float = 5
    max_message_bytes: int = 32 * 1024 * 1024
    #: 发送 / 补发缓冲的上限；连接在时持续满这么久即以 1011 断开（§6.6）
    outbox_bytes: int = 64 * 1024 * 1024
    outbox_full_limit_s: float = 30
    max_pending_asks: int = 8
    ledger_size: int = 16

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> BridgeConfig:
        values: dict[str, Any] = {}
        for name in cls.model_fields:
            raw = env.get(PREFIX + name.upper())
            if raw is None:
                continue
            if name == "agent_cmd":
                values[name] = tuple(_argv(raw))
            elif name == "agent_env_allow":
                values[name] = tuple(v.strip() for v in raw.split(",") if v.strip())
            elif raw == "" and name in _OPTIONAL:
                values[name] = None
            else:
                values[name] = raw
        return cls.model_validate(values)

    def read_token(self) -> str:
        token = self.token_file.read_text().strip()
        if not token:
            raise ValueError(f"token 文件为空：{self.token_file}")
        return token

    def agent_env(self, env: Mapping[str, str]) -> dict[str, str]:
        """按白名单从 bridge 的环境里挑出传给 agent 的变量。"""
        return {k: env[k] for k in self.agent_env_allow if k in env}


def _argv(raw: str) -> list[str]:
    """JSON 数组（推荐，无歧义）或 shell 风格的字符串。"""
    raw = raw.strip()
    if raw.startswith("["):
        value = json.loads(raw)
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError("ATLAS_BRIDGE_AGENT_CMD 必须是字符串数组")
        return value
    return shlex.split(raw)
