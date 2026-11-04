"""沙箱基座：把 OpenSandbox 包成三个领域（生命周期、沙箱内执行、网络与凭据）。

依赖方向：执行 → 生命周期（只用 access）→ 网络；网络经 EgressLocator 反向拿 egress 的地址。
"""
