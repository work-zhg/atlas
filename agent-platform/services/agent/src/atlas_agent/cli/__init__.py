"""CLI 运行时（c4-model/agent/cli-runtime/code-cli-runtime.html）：让会话 Pod 里的外部 CLI 智能体
（如 Claude Code）跑一轮。对 Run 执行器来说它和原生运行时是同一个接口（AgentRuntime）。

四个领域：

    一轮的编排    runtime（CliRuntime）· capabilities · modes · outcome
    上游连接      link（SessionLink、TurnInbox、CliSession）· client（HostClient，atlas.host.v1）
    权限转发      permission（PermissionDesk）
    内容翻译      translate（server 里唯一理解 CLI 内容协议 ACP 的地方）

★ 这里不导入任何子模块：translate 属于纯计算层（import-linter「server 纯计算层不得依赖基础设施」），
  包的 __init__ 一导入 runtime，读 translate 就会把数据库、Web 框架一起拖进来。
"""
