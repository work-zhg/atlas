"""建一个沙箱需要的全部信息：CreateSpec、Labels、Volume、Resources（值对象）。

和 OpenSandbox 的请求格式无关：翻成请求体只在 LifecycleServer 一处做。

★ 类型化、构造时就校验：OpenSandbox 会静默丢掉不认识的字段（把 networkPolicy 拼错就等于出站
  全放行）；metadata 直接成为 K8s label，值超过 63 个字符或含非法字符时创建回 400。
★ template 是模板 code，选的是 Pod 形态而不是 server。
★ 机密环境变量单独放（secret_env）：OpenSandbox 1.1.1 的创建接口只收明文 env，现在由
  LifecycleServer 并进 env；等它支持引用 Secret，只改那一处。
★ 没有 TTL 字段：一律不过期，回收由 Atlas 做。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from pydantic import SecretStr

from ..network.policy import NetworkPolicy

__all__ = [
    "LABEL_PREFIX",
    "SANDBOX_LABEL",
    "TEMPLATE_CODE",
    "CreateSpec",
    "Labels",
    "Resources",
    "Volume",
]

#: 写进沙箱的标签一律带这个前缀
LABEL_PREFIX = "atlas."
#: 建沙箱时由基座加上：对账靠它把 OpenSandbox 上的沙箱和登记对上
SANDBOX_LABEL = "atlas.sandbox"
_MANAGED = {"atlas.managed": "true"}

#: K8s label 值：≤ 63 个字符，字母数字开头结尾，中间可有 -_.
_LABEL_VALUE = re.compile(r"^(?:[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,61}[A-Za-z0-9])?)?$")
#: 标签名（前缀之后的部分）：小写字母数字和 -_.
_LABEL_NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9_.]{0,48}[a-z0-9])?$")
#: 模板 code：小写字母、数字和 -
TEMPLATE_CODE = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class Labels:
    """写进沙箱的标签，只写不查：库里不存，按人、按业务查归通用供给。

    统一加 atlas. 前缀和 atlas.managed=true；建沙箱时基座再加 atlas.sandbox=<sandbox_uuid>。
    """

    pairs: Mapping[str, str] = field(default_factory=lambda: MappingProxyType(dict(_MANAGED)))

    @classmethod
    def of(cls, **pairs: str) -> Labels:
        """Labels.of(user=…) → {"atlas.managed": "true", "atlas.user": …}。"""
        out = dict(_MANAGED)
        for name, value in pairs.items():
            out[LABEL_PREFIX + _checked_name(name)] = _checked_value(name, value)
        return cls(MappingProxyType(out))

    def with_sandbox(self, sandbox_uuid: str) -> Labels:
        value = _checked_value("sandbox", sandbox_uuid)
        return Labels(MappingProxyType({**self.pairs, SANDBOX_LABEL: value}))

    def as_dict(self) -> dict[str, str]:
        return dict(self.pairs)


def _checked_name(name: str) -> str:
    if not _LABEL_NAME.match(name):
        raise ValueError(f"标签名不合法：{name!r}")
    return name


def _checked_value(name: str, value: str) -> str:
    if not isinstance(value, str) or not _LABEL_VALUE.match(value):
        raise ValueError(
            f"标签 {name} 的值不合法（≤63 个字符，只含字母数字和 -_.，字母数字开头结尾）：{value!r}"
        )
    return value


@dataclass(frozen=True)
class Volume:
    """整桶 PVC 上的一个前缀，挂到沙箱里的某个目录（写穿式 S3 CSI）。PVC 由运维预建，不自动建。"""

    name: str
    claim: str
    sub_path: str
    mount_path: str
    read_only: bool = False


@dataclass(frozen=True)
class Resources:
    cpu_request: str
    cpu_limit: str
    memory_request: str
    memory_limit: str


@dataclass(frozen=True)
class CreateSpec:
    """建一个沙箱需要的全部信息。network 必填：每个沙箱都带出站策略。"""

    template: str
    image: str
    entrypoint: tuple[str, ...]
    network: NetworkPolicy
    resources: Resources
    env: Mapping[str, str] = field(default_factory=dict)
    secret_env: Mapping[str, SecretStr] = field(default_factory=dict)
    #: 沙箱里对外服务（bridge）读令牌的环境变量：基座给它另生成一个令牌，和 execd 令牌不是同一个
    service_token_env: str | None = None
    volumes: tuple[Volume, ...] = ()
    labels: Labels = field(default_factory=Labels)

    def __post_init__(self) -> None:
        if not TEMPLATE_CODE.match(self.template):
            raise ValueError(f"模板 code 不合法：{self.template!r}")
        if not self.image:
            raise ValueError("镜像不能为空")
        if not isinstance(self.network, NetworkPolicy):
            raise TypeError("CreateSpec.network 必填：每个沙箱都带出站策略")
        names = [*self.env, *self.secret_env]
        if self.service_token_env:
            names.append(self.service_token_env)
        bad = [n for n in names if not _ENV_NAME.match(n)]
        if bad:
            raise ValueError(f"环境变量名不合法：{bad}")
        dup = {n for n in names if names.count(n) > 1}
        if dup:
            raise ValueError(f"环境变量重复（普通、机密、服务令牌之间不能同名）：{sorted(dup)}")
        mounts = [v.mount_path for v in self.volumes]
        if len(set(mounts)) != len(mounts):
            raise ValueError(f"挂载点重复：{mounts}")
