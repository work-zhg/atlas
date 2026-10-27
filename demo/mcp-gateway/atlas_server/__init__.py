"""atlas-server：Agent 配置、权限主体、MCP 接入、Run 流程。

依赖方向：server → engine（atlas_engine）。★ 不依赖 langchain / langgraph：
交给 engine 的是 ToolSpec / ModelSpec，拿回的是 TurnResult，都是标准库类型。
MCP 协议用 FastMCP 的 client（server 自己负责传输、凭据、会话），不经过 LangChain。
"""
