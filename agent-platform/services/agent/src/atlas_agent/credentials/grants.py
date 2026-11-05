"""CredentialGrants：凭据组件的入口；CredentialProfileStore：档案的来源。

    check(subject)        建会话时校验所选档案：不存在或无权抛 CredentialDenied，建会话直接失败
    resolve(subject)      每轮按当前的档案和规则重新授权，交出 CredentialSet（权限收回这一轮就生效）
    available(user, app)  某人在某个来源应用下能选哪些档案：只有 code、注入方式和主机，不含凭据值

★ 写错或无权一律拒绝，不静默不给：静默的表现是 git 在 Pod 里鉴权失败，原因很难追到这里。
★ 档案来自部署的 Secret（CREDENTIAL_PROFILES），不进数据库：凭据原文进库要先有加密和审计方案。
  仓储接口只有按 code 取和列出，以后换成库或外部 Vault 不影响调用方。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .profiles import (
    PROFILE_CODE,
    AuthKind,
    CredentialProfile,
    CredentialSet,
    CredentialSubject,
    GitAuthor,
    GrantRule,
)

if TYPE_CHECKING:
    from ..config import Settings

__all__ = ["CredentialDenied", "CredentialGrants", "CredentialProfileStore", "ProfileInfo"]


class CredentialDenied(PermissionError):
    """档案不存在，或这个会话无权使用它。"""


@dataclass(frozen=True)
class ProfileInfo:
    """列给建会话的一方看的档案：不含凭据值。"""

    code: str
    auth: str
    hosts: tuple[str, ...]


class CredentialProfileStore:
    """档案的来源：部署 Secret 里的 JSON。

    加载时逐个校验，有一个不合规就启动失败（设置校验时已做）。
    """

    def __init__(self, profiles: dict[str, CredentialProfile]) -> None:
        self._profiles = dict(profiles)

    @classmethod
    def from_settings(cls, settings: Settings) -> CredentialProfileStore:
        profiles: dict[str, CredentialProfile] = {}
        for code, p in settings.credential_profiles.items():
            author = GitAuthor(p.author_name, p.author_email or "") if p.author_name else None
            profiles[_checked(code)] = CredentialProfile(
                code=code,
                auth=AuthKind(p.auth),
                hosts=tuple(p.hosts),
                grant=GrantRule(frozenset(p.users), frozenset(p.apps)),
                _secret=p.secret,
                username=p.username,
                header=p.header,
                author=author,
            )
        return cls(profiles)

    def get(self, code: str) -> CredentialProfile | None:
        return self._profiles.get(code)

    def all(self) -> list[CredentialProfile]:
        return [self._profiles[c] for c in sorted(self._profiles)]


def _checked(code: str) -> str:
    if not PROFILE_CODE.match(code):
        raise ValueError(f"凭据档案 code 不合法（小写字母、数字和 -）：{code!r}")
    return code


class CredentialGrants:
    def __init__(self, store: CredentialProfileStore) -> None:
        self._store = store

    def check(self, subject: CredentialSubject) -> None:
        """建会话时：档案不存在或无权，建会话直接失败。"""
        self._authorized(subject)

    def resolve(self, subject: CredentialSubject) -> CredentialSet:
        """每轮：按当前的档案和规则重新授权。"""
        profiles = self._authorized(subject)
        author = next((p.author for p in profiles if p.author is not None), None)
        return CredentialSet(
            bindings=tuple(p.to_binding() for p in profiles),
            codes=frozenset(p.code for p in profiles),
            git_author=author,
        )

    def available(self, user_uuid: str, source_app: str) -> list[ProfileInfo]:
        return [
            ProfileInfo(p.code, p.auth.value, p.hosts)
            for p in self._store.all()
            if p.grant.allows(user_uuid, source_app)
        ]

    def _authorized(self, subject: CredentialSubject) -> list[CredentialProfile]:
        out = []
        for code in dict.fromkeys(subject.profile_codes):
            profile = self._store.get(code)
            if profile is None:
                raise CredentialDenied(f"凭据档案 {code!r} 不存在")
            if not profile.allows(subject):
                if profile.grant.apps and subject.source_app not in profile.grant.apps:
                    apps = "、".join(sorted(profile.grant.apps))
                    raise CredentialDenied(f"只有 {apps} 建的会话能用凭据档案 {code!r}")
                raise CredentialDenied(f"会话创建者无权使用凭据档案 {code!r}")
            out.append(profile)
        return out
