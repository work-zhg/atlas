"""保管设备令牌（c4-model/agent/connector/code-connector-link.html · CredentialStore）。

★ 优先放系统钥匙串（macOS 的 security 命令）：CLI 以电脑主人的身份跑任意命令，令牌要尽量远离它，
  钥匙串的访问还要经过系统一道。没有钥匙串时退回只有本人可读的文件（0600）。
★ 不放进环境变量：环境变量会被 bridge 和 CLI 继承。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

__all__ = ["CredentialStore", "DeviceCredential"]

_SERVICE = "atlas-connect"


@dataclass(frozen=True)
class DeviceCredential:
    """配对的结果：连哪个平台、我是哪台设备、凭什么连。"""

    gateway: str
    device_uuid: str
    name: str
    token: str


class CredentialStore:
    def __init__(self, path: Path, *, keychain: bool | None = None) -> None:
        self._path = path
        if keychain is None:
            keychain = (
                sys.platform == "darwin"
                and shutil.which("security") is not None
                and os.environ.get("ATLAS_CONNECT_PLAIN_CREDENTIALS") != "1"
            )
        self._keychain = keychain

    def load(self) -> DeviceCredential | None:
        if not self._path.exists():
            return None
        meta = json.loads(self._path.read_text(encoding="utf-8"))
        token = meta.get("token") or (
            self._keychain_get(meta["device_uuid"]) if self._keychain else None
        )
        if not token:
            return None
        return DeviceCredential(meta["gateway"], meta["device_uuid"], meta.get("name", ""), token)

    def save(self, cred: DeviceCredential) -> None:
        meta = asdict(cred)
        if self._keychain:
            self._keychain_set(cred.device_uuid, cred.token)
            meta.pop("token")
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    def clear(self) -> None:
        if not self._path.exists():
            return
        meta = json.loads(self._path.read_text(encoding="utf-8"))
        if self._keychain and meta.get("device_uuid"):
            subprocess.run(
                ["security", "delete-generic-password", "-s", _SERVICE, "-a", meta["device_uuid"]],
                capture_output=True,
                check=False,
            )
        self._path.unlink()

    @staticmethod
    def _keychain_set(account: str, secret: str) -> None:
        subprocess.run(
            ["security", "add-generic-password", "-U", "-s", _SERVICE, "-a", account, "-w", secret],
            capture_output=True,
            check=True,
        )

    @staticmethod
    def _keychain_get(account: str) -> str | None:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", _SERVICE, "-a", account, "-w"],
            capture_output=True,
            text=True,
            check=False,
        )
        return out.stdout.strip() or None if out.returncode == 0 else None
