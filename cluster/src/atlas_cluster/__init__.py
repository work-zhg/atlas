"""Atlas cluster service —— K8s 模板与会话 Pod 生命周期的唯一所有者。

独立成服务而不是 server 的一个包：模板、创建、回收、卷分配、配额这些逻辑
**不是 acp 独有的**（native 的 sandbox Pod 将来要一模一样的东西），且它是
全仓唯一需要 K8s 写权限的进程 —— 独立之后那份 RBAC 只发给它一个。

    schemas   与 server 的 API 契约（纯 pydantic）
    template  Pod / Secret 的 manifest 渲染
    manager   ensure / release / reap + 配额
    backend   ClusterBackend 协议 + 内存实现
    k8s       真集群实现（kubernetes-asyncio 为可选依赖）
    api       FastAPI 面
"""
