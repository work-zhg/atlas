"""出站策略的值对象：NetworkPolicy、EgressRule、MitmTrust，以及翻成请求体的 policy_json。

★ 默认动作总是显式的：sidecar 把 networkPolicy: {} 当作全拒绝（defaultAction 缺省是 deny），
  规范却写着全放行；OpenSandbox 还会静默丢掉拼错的字段。所以策略只能从 deny_all / allow_all
  起步，policy_json 总是写出 defaultAction。
★ 默认放行时，域名拒绝只在 DNS 层生效，直连 IP 绕得过去：真要拦就写 IP / CIDR。集群网段的
  拒绝在 egress 镜像的 deny.always 里（docker/egress），种类只写自己要放行的。
★ 凭据注入（Credential Vault）只能和策略一起开，所以 inject_credentials 放在策略上。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Literal

__all__ = ["EgressRule", "MitmTrust", "NetworkPolicy", "policy_json"]

Action = Literal["allow", "deny"]


@dataclass(frozen=True)
class EgressRule:
    """一条规则：按域名、*. 通配域名、IP 或 CIDR 放行或拒绝。"""

    action: Action
    target: str

    def __post_init__(self) -> None:
        if self.action not in ("allow", "deny"):
            raise ValueError(f"出站规则的动作只能是 allow 或 deny：{self.action!r}")
        if not self.target or self.target != self.target.strip():
            raise ValueError(f"出站规则的目标不能为空或带空白：{self.target!r}")


@dataclass(frozen=True)
class MitmTrust:
    """开了凭据注入，egress 就拦截全部 HTTPS，沙箱里的工具都要信任 MITM CA。

    ★ bootstrap 导出的三个变量（inherited）要原样传给 CLI；git 不认 SSL_CERT_FILE（Debian 的
      libcurl 编译期写死了 CA 路径），要另给 GIT_SSL_CAINFO（P0-3 实测）。
    """

    #: execd 的 bootstrap 把系统根证书与 MITM CA 合并成这份
    bundle: str = "/opt/opensandbox/merged-ca-certificates.pem"
    inherited: tuple[str, ...] = ("NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")

    def env(self) -> dict[str, str]:
        return {"GIT_SSL_CAINFO": self.bundle, "CURL_CA_BUNDLE": self.bundle}


@dataclass(frozen=True)
class NetworkPolicy:
    """一个沙箱的出站策略。用 deny_all() / allow_all() 起步，再 allowing / denying 加规则。"""

    default: Action
    rules: tuple[EgressRule, ...] = ()
    inject_credentials: bool = False

    def __post_init__(self) -> None:
        if self.default not in ("allow", "deny"):
            raise ValueError(f"出站策略的默认动作只能是 allow 或 deny：{self.default!r}")

    @classmethod
    def deny_all(cls) -> NetworkPolicy:
        return cls(default="deny")

    @classmethod
    def allow_all(cls) -> NetworkPolicy:
        return cls(default="allow")

    def allowing(self, *targets: str) -> NetworkPolicy:
        return replace(self, rules=self.rules + tuple(EgressRule("allow", t) for t in targets))

    def denying(self, *targets: str) -> NetworkPolicy:
        return replace(self, rules=self.rules + tuple(EgressRule("deny", t) for t in targets))

    def with_credentials(self) -> NetworkPolicy:
        return replace(self, inject_credentials=True)

    def trust(self) -> MitmTrust | None:
        """开了凭据注入才要信任 MITM CA。"""
        return MitmTrust() if self.inject_credentials else None


def policy_json(policy: NetworkPolicy) -> dict[str, Any]:
    """NetworkPolicy → OpenSandbox 创建请求里的 networkPolicy（+ credentialProxy）两段。

    建沙箱和运行期修改共用这一处。总是写出 defaultAction。
    """
    body: dict[str, Any] = {"networkPolicy": {"defaultAction": policy.default}}
    if policy.rules:
        body["networkPolicy"]["egress"] = [
            {"action": r.action, "target": r.target} for r in policy.rules
        ]
    if policy.inject_credentials:
        body["credentialProxy"] = {"enabled": True}
    return body
