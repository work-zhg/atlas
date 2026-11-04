"""沙箱 · 基座 · 生命周期（c4-model/agent/sandbox/code-sandbox-base-lifecycle.html）。

    spec.py        CreateSpec、Labels、Volume、Resources（值对象）
    state.py       SandboxState：状态分组只写一份
    server.py      LifecycleServer：一个 lifecycle server 的 HTTP 客户端和翻译
    runtime.py     SandboxRuntime 与 OpenSandboxRuntime：按 sandbox_uuid 建、查、删、对账
    templates.py   沙箱模板：TemplatePolicy、SandboxTemplates、TemplatePublisher
    kube.py        模板发布用的最小 K8s 客户端

★ 不在这里导入子模块：db.models 要用 state.py，而 runtime.py 又要用 db.models。
"""
