"""调用前按工具的参数定义校验参数（MCP 方案设计 §6.2）。

缺必填项、类型不对、多了定义里没有的字段，直接把错误交回模型，不发请求。

★ 为什么要在 Atlas 侧做：模型生成的参数只受提示词约束；服务自己校不校验因服务而异，
  网关从 REST 转出来的服务可能直接把错误参数传给后端。LangChain 对字典形式的参数定义
  不做校验（原样透传），langchain.mcp 也没有。
★ 不做约束解码（模型厂商的严格模式）：第三方服务给的参数定义经常不在它支持的范围内。
★ 参数定义本身不合法时不校验（记一条告警）：那是服务的问题，不该让每次调用都失败。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["argument_problems"]

#: 最多报几条：够模型改正，不把整份校验结果塞进上下文
_MAX_PROBLEMS = 5


def argument_problems(schema: Mapping[str, Any], arguments: Mapping[str, Any]) -> list[str]:
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError
    from jsonschema.validators import validator_for

    problems: list[str] = []
    props = schema.get("properties")
    # 没写 additionalProperties = 按定义来：多出来的字段多半是模型自己编的。
    # 写了的（false / true / 子定义）交给下面的 JSON Schema 校验
    if isinstance(props, Mapping) and "additionalProperties" not in schema:
        extra = sorted(k for k in arguments if k not in props)
        if extra:
            problems.append(f"多了参数定义里没有的字段 {', '.join(extra)}")
    cls = validator_for(schema, default=Draft202012Validator)
    try:
        cls.check_schema(schema)
    except SchemaError as exc:
        logger.warning("MCP 工具的参数定义不合法，跳过参数校验：%s", exc.message)
        return problems
    for error in sorted(cls(schema).iter_errors(dict(arguments)), key=lambda e: list(e.path)):
        where = ".".join(str(p) for p in error.path)
        problems.append(f"{where}：{error.message}" if where else error.message)
    return problems[:_MAX_PROBLEMS]
