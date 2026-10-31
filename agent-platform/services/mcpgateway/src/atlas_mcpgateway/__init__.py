"""MCP 网关（原型 agent-platform/doc/prototype/mcp-gateway.html）：所有 MCP 调用的唯一入口。

    控制面   服务注册表：MCP 代理（从上游拉工具定义）、REST 转换（OpenAPI 或手工）、
             gRPC 转换（反射或描述文件）；上游凭据；API Key（用户 Key、智能体 Key，
             各有一个用户中心主体）；在用户中心登记服务和工具的数据对象、给 Key 授权；
             给 Atlas 的元数据与「这把 Key 能用什么」
    数据面   网关自己就是 MCP 服务端（/mcp/{服务名}，无状态）：认 Key → 判权（以 Key 的主体作
             X-Auth-Uid 调用户中心，按 Key 短缓存）→ 校验参数 → 按服务类型调上游（注入上游凭据）

★ 上游凭据只在网关库里，不回显、不下发给 Atlas 和 Pod；调用方只带自己的 Key。
★ 流量治理（并发、限流、超时、熔断）是通用组件，单独设计，本服务暂不做。
"""
