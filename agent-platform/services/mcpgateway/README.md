# atlas-mcpgateway

MCP 网关：MCP 服务的唯一入口——连 MCP Server、把 REST / gRPC 转成工具、API Key、按授权调用。

## 用户中心登记

代码里只有判断用到的编码（`atlas_mcpgateway/auth.py`、`perms.py`），不往用户中心登记。下面这些由
运营人员在运营中心「应用目录」里录入；编码必须和这里一致，名称、角色怎么组合可以按需调整。

**应用**

| 编码 | 名称 | 说明 |
|---|---|---|
| `mcp-gateway` | MCP 网关 | MCP 服务的唯一入口：连 MCP Server、把 REST / gRPC 转成工具、API Key、按授权调用 |

应用编码由配置 `ATLAS_MCPGW_UC_APP_CODE` 决定（默认 `mcp-gateway`）。

**操作码**

| 模块 | 编码 | 名称 |
|---|---|---|
| MCP 服务 | `mcp:manage` | 登记与停用 MCP 服务、改连接和工具、管理 API Key |

**数据编码**

| 编码 | 名称 | 说明 |
|---|---|---|
| `McpServer` | MCP 服务 | 一个 MCP 服务一条：只读授给 API Key = 能用全部工具（含以后新增的）；Owner 授给人 = 所有者（能给 Key 授权） |
| `McpTool` | MCP 工具 | 一个工具一条（服务名/工具名）：只读授给 API Key = 只能用这个工具；Owner 和服务的所有者保持一致 |

**建议的内置角色**

| 编码 | 名称 | 操作码 |
|---|---|---|
| `MCP_ADMIN` | MCP 管理员 | `mcp:manage` |

MCP 管理员要给不是自己的服务授权，还要在租户的用户中心里同时拿到「全部 MCP 服务 · 所有者」和
「全部 MCP 工具 · 所有者」两条数据权限（以前由 register-uc 把后一条跟着前一条同步，现在由租户
管理员一起授）。
