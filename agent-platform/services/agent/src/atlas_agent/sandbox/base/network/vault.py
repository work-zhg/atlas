"""CredentialBinding：一份凭据和它适用的主机，以及翻成 Credential Vault 请求体的 vault_body。

请求发往这些主机时，egress 按 auth 指定的方式注入：basic（Authorization: Basic）、bearer
（Authorization: Bearer）、api_key（凭据放进一个请求头）。凭据值只出现在这里和写入请求里，
egress 的读接口不返回它。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import SecretStr

__all__ = ["CredentialBinding", "vault_body"]

BindingAuth = Literal["basic", "bearer", "api_key"]


@dataclass(frozen=True)
class CredentialBinding:
    name: str
    hosts: tuple[str, ...]
    auth: BindingAuth
    secret: SecretStr = field(repr=False)
    #: api_key 注入到哪个请求头
    header: str | None = None

    def __post_init__(self) -> None:
        if not self.hosts:
            raise ValueError(f"凭据 {self.name} 没有适用的主机")
        if self.auth == "api_key" and not self.header:
            raise ValueError(f"凭据 {self.name} 是 api_key，要给出注入的请求头")

    @classmethod
    def basic(
        cls, name: str, hosts: list[str], username: str, token: SecretStr
    ) -> CredentialBinding:
        """git over HTTPS 就是这一种。Vault 要预先 base64 编码的 user:password。"""
        raw = f"{username}:{token.get_secret_value()}".encode()
        return cls(name, tuple(hosts), "basic", SecretStr(base64.b64encode(raw).decode()))

    @classmethod
    def bearer(cls, name: str, hosts: list[str], token: SecretStr) -> CredentialBinding:
        return cls(name, tuple(hosts), "bearer", token)

    @classmethod
    def api_key(
        cls, name: str, hosts: list[str], key: SecretStr, *, header: str
    ) -> CredentialBinding:
        return cls(name, tuple(hosts), "api_key", key, header=header)

    def credential_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": {"type": "inline", "value": self.secret.get_secret_value()},
        }

    def binding_json(self) -> dict[str, Any]:
        auth: dict[str, Any] = {"credential": self.name}
        if self.auth == "api_key":
            auth |= {"type": "apiKey", "name": self.header}
        else:
            auth["type"] = self.auth
        return {"name": self.name, "match": {"hosts": list(self.hosts)}, "auth": auth}


def vault_body(bindings: list[CredentialBinding]) -> dict[str, Any]:
    """整份写入 Credential Vault 的请求体（POST /credential-vault）。"""
    return {
        "credentials": [b.credential_json() for b in bindings],
        "bindings": [b.binding_json() for b in bindings],
    }
