"""ACP v1 协议模型（代码设计 §3）。

模型由官方 schema 生成（``_generated.py``，不要手改）；本模块只重新导出 bridge 与 server
实际用到的类型，外加少量手写辅助（``caps``、``updates``、``errors``）。

联合类型的每个变体都继承规范里有名字的类型（例如 tool_call 变体继承 ``ToolCall``），
判断类型时用这些有名字的类，不要依赖生成器编号出来的类名（``SessionUpdate4`` 之类）。
"""

from . import methods
from ._generated import (
    AgentCapabilities,
    CancelNotification,
    ClientCapabilities,
    ContentChunk,
    Diff,
    Implementation,
    InitializeRequest,
    InitializeResponse,
    LoadSessionRequest,
    McpServerHttp,
    McpServerSse,
    McpServerStdio,
    NewSessionRequest,
    NewSessionResponse,
    PermissionOption,
    PermissionOptionKind,
    Plan,
    PromptRequest,
    PromptResponse,
    RequestPermissionRequest,
    RequestPermissionResponse,
    ResumeSessionRequest,
    SelectedPermissionOutcome,
    SessionNotification,
    StopReason,
    TextContent,
    ToolCall,
    ToolCallStatus,
    ToolCallUpdate,
    ToolKind,
)
from .caps import AgentCaps
from .errors import AcpErrorCode
from .updates import TOOL_FINISHED, TOOL_UPDATE_KINDS, ToolStatus, tool_status, update_kind

__all__ = [
    "TOOL_FINISHED",
    "TOOL_UPDATE_KINDS",
    "AcpErrorCode",
    "AgentCapabilities",
    "AgentCaps",
    "CancelNotification",
    "ClientCapabilities",
    "ContentChunk",
    "Diff",
    "Implementation",
    "InitializeRequest",
    "InitializeResponse",
    "LoadSessionRequest",
    "McpServerHttp",
    "McpServerSse",
    "McpServerStdio",
    "NewSessionRequest",
    "NewSessionResponse",
    "PermissionOption",
    "PermissionOptionKind",
    "Plan",
    "PromptRequest",
    "PromptResponse",
    "RequestPermissionRequest",
    "RequestPermissionResponse",
    "ResumeSessionRequest",
    "SelectedPermissionOutcome",
    "SessionNotification",
    "StopReason",
    "TextContent",
    "ToolCall",
    "ToolCallStatus",
    "ToolCallUpdate",
    "ToolKind",
    "ToolStatus",
    "methods",
    "tool_status",
    "update_kind",
]
