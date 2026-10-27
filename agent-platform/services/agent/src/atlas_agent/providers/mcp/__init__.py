"""MCP 工具接入（doc/mcp-design.html 第二版）：Atlas 只连自研 MCP 网关。

三条边界：
1. **只经 MCP 网关**：服务、工具定义、复核结论都从网关读；调用经网关、带这一轮权限主体的
   API Key。Atlas 不自己发现工具，看不到 Higress，不持有后端凭据。
2. **只做远程**（Streamable HTTP），不在平台里拉起 stdio 进程。
3. **engine 不认识 MCP**：这里把工具解析成 BaseTool 以 extra_tools 注入。

  gateway.py    网关的内部接口：元数据（GatewayCatalog）、主体能用什么（SubjectAccess）
  catalog.py    目录的形状：工具定义、服务快照、目录协议
  config.py     网关上的一个服务、连它的 MCP 客户端
  native.py     native agent 的装配与调用链（langchain.mcp + FastMCP client）
  session.py    一个 Run 段的会话复用、主体 Key、「用不了」的记忆
  arguments.py  调用前的参数校验
  acp.py        acp 会话的下发（网关地址 + 主体 Key）

命名规则（三种名字）在 domain/mcp_naming.py —— build.py 要用它，而 build.py
只允许 import 纯层。
"""

from ...domain.mcp_naming import MCP_PREFIX, model_name, parse_tool_id, tool_id
from .catalog import McpCatalog, McpToolDef, McpUnavailable, ServerSnapshot
from .config import McpServerConfig
from .gateway import (
    GatewayCatalog,
    GatewayUnavailable,
    SubjectAccess,
    make_mcp_catalog,
    mcp_subject_access,
)
from .native import CallContext, MissingTool, NativeMcpTools
from .review import ReviewIndex, review_status
from .session import KeyAuth, McpSessions

__all__ = [
    "MCP_PREFIX",
    "CallContext",
    "GatewayCatalog",
    "GatewayUnavailable",
    "KeyAuth",
    "McpCatalog",
    "McpServerConfig",
    "McpSessions",
    "McpToolDef",
    "McpUnavailable",
    "MissingTool",
    "NativeMcpTools",
    "ReviewIndex",
    "ServerSnapshot",
    "SubjectAccess",
    "make_mcp_catalog",
    "mcp_subject_access",
    "model_name",
    "parse_tool_id",
    "review_status",
    "tool_id",
]
