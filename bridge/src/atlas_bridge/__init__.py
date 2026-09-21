"""Pod 内的 ACP bridge。

依赖只有 atlas_acp 与 websockets —— **不认识 atlas_server**。与 server 的
唯一接口是 WS 上的线协议；代码级共享会让「升级 server 必须同步升级全部
在跑的 Pod」，而 Pod 在跑时没法原地升级（acp 详设 §03）。

    adapter    stdio 上驱动 CLI adapter 的子进程
    ws         WS 端点：握手鉴权 · 单活跃 prompt 门 · 权限转交
    lifecycle  优雅终止三步
    main       入口
"""
