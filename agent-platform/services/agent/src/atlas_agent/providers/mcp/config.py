"""MCP server 的连接配置与凭据占位符解析。

★ 凭据用 ${ENV} 占位符，真值在服务器环境变量里。§14 要求凭据不进
  `agent_version.spec` —— spec 会被 API 原样返回给前端，写进去等于公开。
  这里连配置本身都不存密文：只存占位符，用时才解析。
"""

from __future__ import annotations

import os
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from ...domain.mcp_naming import SERVER_NAME_PATTERN
from ...errors import CapabilityUnavailable

#: ${VAR} 占位符
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class McpServerConfig(BaseModel):
    """一个远程 MCP server。

    headers 里可以写 `{"Authorization": "Bearer ${MY_TOKEN}"}` ——
    真值从环境变量取，配置本身不含密文。

    ★ name 不许含下划线：模型侧名是 `<server>__<tool>`，server 名里有
      `_` 的话分隔点就不唯一了，审计时无法从模型侧名还原来源（MCP 详设 §04）。
    """

    name: str = Field(pattern=SERVER_NAME_PATTERN)
    url: str
    transport: str = "streamable_http"
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    #: 本 server 单次 tools/call 的上限，None = 用全局 MCP_CALL_TIMEOUT_S。
    #: ★ 按 server 配而不是只有全局：各家延迟分布差得很远 —— serpapi 对没有
    #:   缓存的查询偶尔 50s+，全局调到它能接受的值，会让其它 server 挂住时
    #:   模型陪着干等同样久。
    call_timeout_s: float | None = Field(default=None, gt=0)
    #: 凭据作用域。platform = 平台统一凭据；user = 每个用户自己授权（OAuth，P3）
    credential_scope: Literal["platform", "user"] = "platform"
    #: 工具定义是否要人工复核后才进模型上下文（技能 / MCP 设计 §8.3）。
    #: ★ 环境变量里的 server 默认不要求：它们在复核机制出现之前就在用了。
    review_required: bool = False


class MissingCredential(CapabilityUnavailable):
    """占位符引用的环境变量不存在。

    ★ 不能静默把它当空串：那会用一个空 token 去连，服务端返回 401，
      报错指向「认证失败」而不是「你没配这个环境变量」（§13.2）。
    """


def resolve_env_refs(value: str) -> str:
    def _sub(match: re.Match[str]) -> str:
        var = match.group(1)
        found = os.environ.get(var)
        if found is None:
            raise MissingCredential(f"环境变量 {var} 未设置（被 MCP 配置引用）")
        return found

    return _ENV_REF.sub(_sub, value)


def connection_for(server: McpServerConfig, *, timeout_s: float) -> dict[str, Any]:
    """adapter 的连接参数。占位符在这里解析 —— 真值只活在这个 dict 里。"""
    return {
        "url": resolve_env_refs(server.url),
        "transport": server.transport,
        "headers": {k: resolve_env_refs(v) for k, v in server.headers.items()},
        "timeout": timeout_s,
    }
