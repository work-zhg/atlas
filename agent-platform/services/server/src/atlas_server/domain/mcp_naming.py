"""MCP 工具的三种名字（MCP 详设 §04）。

  spec 标识   mcp:github:search   spec.tool_names、/v1/tools.name
  模型侧名    github__search      LLM 工具定义、require_approval_for、tool.* 事件
  线上名      search              tools/call 发给 server

此前模型看到的是裸线上名：两个 server 的同名工具互相覆盖，与内置工具
撞名，而且 require_approval_for 永远匹配不到 MCP 工具。
"""

from __future__ import annotations

import re

MCP_PREFIX = "mcp:"
SEP = "__"

#: server 名：不含 `_`（保证 SEP 第一次出现处就是分隔点），限 24 字符
#: （给工具名留出 ≥38 字符）。
SERVER_NAME_PATTERN = r"^[a-z][a-z0-9-]{0,23}$"

#: Anthropic 与 OpenAI 工具名规则的共同子集
_MODEL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def tool_id(server: str, tool: str) -> str:
    return f"{MCP_PREFIX}{server}:{tool}"


def parse_tool_id(value: str) -> tuple[str, str] | None:
    """`mcp:<server>:<tool>` → (server, tool)。不是 MCP 工具则返回 None。"""
    if not value.startswith(MCP_PREFIX):
        return None
    rest = value[len(MCP_PREFIX) :]
    server, _, tool = rest.partition(":")
    return (server, tool) if server and tool else None


def model_name(server: str, tool: str) -> str | None:
    """模型侧名。线上名不合规（字符集 / 超长）时返回 None —— 该工具不可用。"""
    name = f"{server}{SEP}{tool}"
    return name if _MODEL_NAME.match(name) else None


def approval_target(name: str) -> str:
    """require_approval_for 的一项 → 审批中间件实际匹配的模型侧名。

    约定存的是模型侧名（编辑器从 /v1/tools 的 model_tool_names 取）。但编辑器
    此前提示用户手填 `mcp:server:tool` —— 那种写法从来没匹配上过任何调用。
    这里把它翻译过来，让既有配置按用户本意生效，而不是继续静默失效。
    """
    parsed = parse_tool_id(name)
    if parsed is None:
        return name
    return model_name(*parsed) or name
