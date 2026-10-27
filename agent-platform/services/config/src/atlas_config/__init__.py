"""Atlas 配置服务：技能（与持久指令）的唯一所有者。MCP 注册表已迁到 MCP 网关（atlas_mcpgateway）。

运行时（atlas_agent）只读消费这里的已发布内容，调用方向只有 server → config。
对外契约只有两样：HTTP 接口（api/internal.py）与 `atlas_config.schemas`。
"""
