"""凭据组件的领域对象：CredentialProfile（聚合根）、GrantRule、CredentialSubject、CredentialSet。

c4-model/agent/code-credentials.html。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import SecretStr

from ..sandbox.base.network.vault import CredentialBinding

if TYPE_CHECKING:
    from ..db.models import Thread

__all__ = [
    "PROFILE_CODE",
    "AuthKind",
    "CredentialProfile",
    "CredentialSet",
    "CredentialSubject",
    "GitAuthor",
    "GrantRule",
]

#: 档案 code：小写字母、数字和 -
PROFILE_CODE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class AuthKind(StrEnum):
    """注入方式，和 egress Credential Vault 支持的三种一一对应。"""

    BASIC = "basic"
    BEARER = "bearer"
    API_KEY = "api_key"


@dataclass(frozen=True)
class GrantRule:
    """谁能用：限定用户、限定来源应用，至少写一项；两项都写时都要满足。

    ★ 两项都没写的档案在加载时报错，不当成「谁都能用」——那等于把仓库的写权限给了所有人。
    """

    users: frozenset[str] = frozenset()
    apps: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.users and not self.apps:
            raise ValueError("授权规则至少限定用户或来源应用之一")

    def allows(self, user_uuid: str, source_app: str) -> bool:
        if self.users and user_uuid not in self.users:
            return False
        return not (self.apps and source_app not in self.apps)


@dataclass(frozen=True)
class GitAuthor:
    """会话 Pod 里 git 提交的作者（用这份凭据推代码的机器人账号）。"""

    name: str
    email: str


@dataclass(frozen=True)
class CredentialSubject:
    """谁要用凭据：会话的创建人、建会话的来源应用、会话选的档案。档案在建会话时定、之后不改。"""

    user_uuid: str
    source_app: str
    profile_codes: tuple[str, ...]

    @classmethod
    def of(cls, thread: Thread) -> CredentialSubject:
        return cls(
            user_uuid=str(thread.created_by_uuid),
            source_app=thread.source_app or "",
            profile_codes=tuple(thread.credential_profiles or ()),
        )


@dataclass(frozen=True)
class CredentialProfile:
    """一份出站凭据档案。凭据值是私有字段，只在 to_binding 时交给基座。"""

    code: str
    auth: AuthKind
    hosts: tuple[str, ...]
    grant: GrantRule
    _secret: SecretStr = field(repr=False)
    username: str | None = None
    header: str | None = None
    author: GitAuthor | None = None

    def allows(self, subject: CredentialSubject) -> bool:
        return self.grant.allows(subject.user_uuid, subject.source_app)

    def to_binding(self) -> CredentialBinding:
        hosts = list(self.hosts)
        if self.auth is AuthKind.BASIC:
            return CredentialBinding.basic(self.code, hosts, self.username or "", self._secret)
        if self.auth is AuthKind.BEARER:
            return CredentialBinding.bearer(self.code, hosts, self._secret)
        return CredentialBinding.api_key(self.code, hosts, self._secret, header=self.header or "")


@dataclass(frozen=True)
class CredentialSet:
    """一个会话这一轮可用的全部凭据：已经授权过、转成了 egress 能写入的绑定。空集合 = 不用凭据。

    codes 用来比较两个集合：以后共享沙箱，只能在凭据集合相同的业务之间进行。
    """

    bindings: tuple[CredentialBinding, ...] = ()
    codes: frozenset[str] = frozenset()
    #: 档案里配了 git 提交作者时，取第一个
    git_author: GitAuthor | None = None

    def empty(self) -> bool:
        return not self.bindings
