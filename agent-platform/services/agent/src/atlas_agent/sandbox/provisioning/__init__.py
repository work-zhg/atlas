"""沙箱 · 通用供给（c4-model/agent/sandbox/code-sandbox-provisioning.html）：按人管理沙箱。

    model.py     BizRef、SandboxRef、SandboxAccount（聚合根）、UserSandbox、
                 QuotaPolicy、AllocationPolicy
    service.py   SandboxProvisioning（唯一入口）、SandboxKind（业务类型的扩展点）

★ 只被原生沙箱、会话 Pod 两个领域调用；上层只拿 sandbox_uuid。
★ 不在这里导入子模块：repositories.sandbox_account 要用 model.py，service.py 又要用它。
"""
