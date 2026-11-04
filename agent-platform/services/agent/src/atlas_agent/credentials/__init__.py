"""凭据组件（c4-model/agent/code-credentials.html）：一个会话这一轮能用哪些出站凭据。

档案描述一份凭据：发往哪些主机、按什么方式注入（basic、bearer、api_key）、凭据值，以及谁能用。
会话在建的时候选档案（这时校验一次），之后每轮由 CLI 运行时再解析一次，得到已授权的凭据集合，
交给会话 Pod，由基座写进沙箱 egress 的 Credential Vault。凭据值不进 Pod。

它不管凭据怎么注入（基座的网络与凭据领域管），也不管 Pod 怎么建（会话 Pod 管）。
"""

from .grants import CredentialDenied, CredentialGrants, CredentialProfileStore, ProfileInfo
from .profiles import (
    AuthKind,
    CredentialProfile,
    CredentialSet,
    CredentialSubject,
    GitAuthor,
    GrantRule,
)

__all__ = [
    "AuthKind",
    "CredentialDenied",
    "CredentialGrants",
    "CredentialProfile",
    "CredentialProfileStore",
    "CredentialSet",
    "CredentialSubject",
    "GitAuthor",
    "GrantRule",
    "ProfileInfo",
]
