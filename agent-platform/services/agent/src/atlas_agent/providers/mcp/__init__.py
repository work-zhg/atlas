"""MCP 工具接入（MCP 详设）。

三条边界：
1. **只做 HTTP/SSE 远程传输**，不做 stdio —— 进程内执行器管子进程的启停
   与回收，运维复杂度明显上升。远程只是发请求。
2. **凭据用 ${ENV} 占位符**（config.py）。
3. **engine 不认识 MCP**。这里把工具解析成 BaseTool 以 extra_tools 注入。

  config.py    server 连接配置、占位符解析
  catalog.py   工具目录：发现与调用分离，定义缓存在 Redis
  native.py    native agent 的装配与调用期 interceptor 链

命名规则（三种名字）在 domain/mcp_naming.py —— build.py 要用它，而 build.py
只允许 import 纯层。
"""

from ...domain.mcp_naming import MCP_PREFIX, model_name, parse_tool_id, tool_id
from .catalog import McpCatalog, McpToolDef, McpUnavailable, ServerSnapshot, SettingsCatalog
from .config import McpServerConfig, MissingCredential, resolve_env_refs
from .native import CallContext, MissingTool, NativeMcpTools
from .review import ReviewIndex, review_status

__all__ = [
    "MCP_PREFIX",
    "CallContext",
    "McpCatalog",
    "McpServerConfig",
    "McpToolDef",
    "McpUnavailable",
    "MissingCredential",
    "MissingTool",
    "NativeMcpTools",
    "ReviewIndex",
    "ServerSnapshot",
    "SettingsCatalog",
    "model_name",
    "parse_tool_id",
    "resolve_env_refs",
    "review_status",
    "tool_id",
]
