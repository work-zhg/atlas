"""agent 能力位的解读（Bridge 设计 §2.5 · §5.6）。

规范：请求 / 响应里**没有出现**的能力一律视为不支持；``sessionCapabilities`` 下的
``resume`` / ``close`` 等是对象，**出现即支持**（``{}`` 也算），不是布尔值。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from ._generated import AgentCapabilities

__all__ = ["AgentCaps"]


@dataclass(frozen=True, slots=True)
class AgentCaps:
    load_session: bool = False
    resume: bool = False
    close: bool = False
    list_sessions: bool = False
    delete: bool = False
    mcp_http: bool = False
    mcp_sse: bool = False
    prompt_image: bool = False
    prompt_audio: bool = False
    prompt_embedded_context: bool = False

    @classmethod
    def from_model(cls, caps: AgentCapabilities | None) -> AgentCaps:
        if caps is None:
            return cls()
        session = caps.session_capabilities
        mcp = caps.mcp_capabilities
        prompt = caps.prompt_capabilities
        return cls(
            load_session=bool(caps.load_session),
            resume=session is not None and session.resume is not None,
            close=session is not None and session.close is not None,
            list_sessions=session is not None and session.list is not None,
            delete=session is not None and session.delete is not None,
            mcp_http=bool(mcp and mcp.http),
            mcp_sse=bool(mcp and mcp.sse),
            prompt_image=bool(prompt and prompt.image),
            prompt_audio=bool(prompt and prompt.audio),
            prompt_embedded_context=bool(prompt and prompt.embedded_context),
        )

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any] | None) -> AgentCaps:
        """从 ``initialize`` 响应里的 ``agentCapabilities`` 原始 JSON 解读。

        形状不合法时按「什么都不支持」处理 —— 能力位宁可少信，不可多信：
        多信一项，bridge 就可能调用一个 agent 并不支持的方法。
        """
        if not raw:
            return cls()
        try:
            return cls.from_model(AgentCapabilities.model_validate(raw))
        except ValidationError:
            return cls()
