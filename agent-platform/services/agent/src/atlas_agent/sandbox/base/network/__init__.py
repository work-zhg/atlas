"""沙箱 · 基座 · 网络与凭据（c4-model/agent/sandbox/code-sandbox-base-network.html）。

    policy.py   NetworkPolicy、EgressRule、MitmTrust、policy_json：出站策略的值对象和翻译
    vault.py    CredentialBinding、vault_body：一份凭据和它适用的主机
    egress.py   EgressControl、EgressLocator、OpenSandboxEgress：运行期写凭据、改策略

★ 它不决定哪个会话能用哪份凭据（凭据组件的 CredentialGrants 决定），也不决定某种沙箱该放行
  什么（种类决定）；它只保证策略的语义明确、凭据写得进去。
"""

from .policy import EgressRule, MitmTrust, NetworkPolicy, policy_json
from .vault import CredentialBinding, vault_body

__all__ = [
    "CredentialBinding",
    "EgressRule",
    "MitmTrust",
    "NetworkPolicy",
    "policy_json",
    "vault_body",
]
