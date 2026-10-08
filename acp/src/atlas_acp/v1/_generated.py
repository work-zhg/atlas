# 由 acp/scripts/generate_v1.py 从官方 ACP schema（schema-v1.23.0，sha256 3c17bd6385d90cf6…）生成。
# 不要手改：升级 ACP 时更新 acp/schema/v1/ 后重新运行生成脚本。

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import AnyUrl, BaseModel, ConfigDict, Field, RootModel, conint


class Jsonrpc(StrEnum):
    field_2_0 = "2.0"


class AgentClientProtocol23(BaseModel):
    """
    A message (request, response, or notification) with `"jsonrpc": "2.0"` specified as
    [required by JSON-RPC 2.0 Specification][1].

    [1]: https://www.jsonrpc.org/specification#compatibility
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    jsonrpc: Jsonrpc


class AgentClientProtocol53(BaseModel):
    """
    A message (request, response, or notification) with `"jsonrpc": "2.0"` specified as
    [required by JSON-RPC 2.0 Specification][1].

    [1]: https://www.jsonrpc.org/specification#compatibility
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    jsonrpc: Jsonrpc


class ReadTextFileRequest(BaseModel):
    """
    Request to read content from a text file.

    Only available if the client supports the `fs.readTextFile` capability.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    session_id: str = Field(
        ..., alias="sessionId", description="The session ID for this request."
    )
    path: str = Field(..., description="Absolute path to the file to read.")
    line: conint(ge=0) | None = Field(
        None, description="Line number to start reading from (1-based)."
    )
    limit: conint(ge=0) | None = Field(
        None, description="Maximum number of lines to read."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class ToolKind(StrEnum):
    """
    Categories of tools that can be invoked.

    Tool kinds help clients choose appropriate icons and optimize how they
    display tool execution progress.

    See protocol docs: [Creating](https://agentclientprotocol.com/protocol/tool-calls#creating)
    """

    read = "read"
    edit = "edit"
    delete = "delete"
    move = "move"
    search = "search"
    execute = "execute"
    think = "think"
    fetch = "fetch"
    switch_mode = "switch_mode"
    other = "other"


class ToolCallStatus(StrEnum):
    """
    Execution status of a tool call.

    Tool calls progress through different statuses during their lifecycle.

    See protocol docs: [Status](https://agentclientprotocol.com/protocol/tool-calls#status)
    """

    pending = "pending"
    in_progress = "in_progress"
    completed = "completed"
    failed = "failed"


class Role(StrEnum):
    """
    The sender or recipient of messages and data in a conversation.
    """

    assistant = "assistant"
    user = "user"


class TextResourceContents(BaseModel):
    """
    Text-based resource contents.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    mime_type: str | None = Field(
        None,
        alias="mimeType",
        description="MIME type describing the encoded media payload.",
    )
    text: str = Field(..., description="Text payload carried by this content block.")
    uri: str = Field(
        ..., description="URI associated with this resource or media payload."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class BlobResourceContents(BaseModel):
    """
    Binary resource contents.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    blob: str = Field(
        ..., description="Base64-encoded bytes for a binary resource payload."
    )
    mime_type: str | None = Field(
        None,
        alias="mimeType",
        description="MIME type describing the encoded media payload.",
    )
    uri: str = Field(
        ..., description="URI associated with this resource or media payload."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class Diff(BaseModel):
    """
    A diff representing file modifications.

    Shows changes to files in a format suitable for display in the client UI.

    See protocol docs: [Content](https://agentclientprotocol.com/protocol/tool-calls#content)
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    path: str = Field(..., description="The absolute file path being modified.")
    old_text: str | None = Field(
        None, alias="oldText", description="The original content (None for new files)."
    )
    new_text: str = Field(
        ..., alias="newText", description="The new content after modification."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class Terminal(BaseModel):
    """
    Embed a terminal created with `terminal/create` by its id.

    The terminal must be added before calling `terminal/release`.

    See protocol docs: [Terminal](https://agentclientprotocol.com/protocol/terminals)
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    terminal_id: str = Field(
        ...,
        alias="terminalId",
        description="Identifier of the terminal instance to embed in the content stream.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class ToolCallLocation(BaseModel):
    """
    A file location being accessed or modified by a tool.

    Enables clients to implement "follow-along" features that track
    which files the agent is working with in real-time.

    See protocol docs: [Following the Agent](https://agentclientprotocol.com/protocol/tool-calls#following-the-agent)
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    path: str = Field(
        ..., description="The absolute file path being accessed or modified."
    )
    line: conint(ge=0) | None = Field(
        None, description="Optional line number within the file."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class PermissionOptionKind(StrEnum):
    """
    The type of permission option being presented to the user.

    Helps clients choose appropriate icons and UI treatment.
    """

    allow_once = "allow_once"
    allow_always = "allow_always"
    reject_once = "reject_once"
    reject_always = "reject_always"


class EnvVariable(BaseModel):
    """
    An environment variable to set when launching an MCP server.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    name: str = Field(..., description="The name of the environment variable.")
    value: str = Field(
        ..., description="The value to set for the environment variable."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class TerminalOutputRequest(BaseModel):
    """
    Request to get the current output and status of a terminal.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    session_id: str = Field(
        ..., alias="sessionId", description="The session ID for this request."
    )
    terminal_id: str = Field(
        ...,
        alias="terminalId",
        description="The ID of the terminal to get output from.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class ReleaseTerminalRequest(BaseModel):
    """
    Request to release a terminal and free its resources.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    session_id: str = Field(
        ..., alias="sessionId", description="The session ID for this request."
    )
    terminal_id: str = Field(
        ..., alias="terminalId", description="The ID of the terminal to release."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class WaitForTerminalExitRequest(BaseModel):
    """
    Request to wait for a terminal command to exit.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    session_id: str = Field(
        ..., alias="sessionId", description="The session ID for this request."
    )
    terminal_id: str = Field(
        ..., alias="terminalId", description="The ID of the terminal to wait for."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class KillTerminalRequest(BaseModel):
    """
    Request to kill a terminal without releasing it.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    session_id: str = Field(
        ..., alias="sessionId", description="The session ID for this request."
    )
    terminal_id: str = Field(
        ..., alias="terminalId", description="The ID of the terminal to kill."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class CreateElicitationRequest13(BaseModel):
    """
    Form-based elicitation where the client renders a form from the provided schema.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    message: str = Field(
        ..., description="A human-readable message describing what input is needed."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nOptional. Omitted and `null` are equivalent and mean no metadata.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )
    mode: Literal["form"]


class CreateElicitationRequest23(BaseModel):
    """
    URL-based elicitation where the client directs the user to a URL.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    message: str = Field(
        ..., description="A human-readable message describing what input is needed."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nOptional. Omitted and `null` are equivalent and mean no metadata.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )
    mode: Literal["url"]


class ElicitationSessionScope(BaseModel):
    """
    Session-scoped elicitation, optionally tied to a specific tool call.

    When `tool_call_id` is set, the elicitation is tied to a specific tool call.
    This is useful when an agent receives an elicitation from an MCP server
    during a tool call and needs to redirect it to the user.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    session_id: str = Field(
        ..., alias="sessionId", description="The session this elicitation is tied to."
    )
    tool_call_id: str | None = Field(
        None,
        alias="toolCallId",
        description="Optional tool call within the session.\n\nOptional. Omitted and `null` are equivalent and mean the elicitation is scoped to the\nsession without a specific tool call.",
    )


class ElicitationRequestScope(BaseModel):
    """
    Request-scoped elicitation, tied to a specific JSON-RPC request outside of a session
    (e.g., during auth/configuration phases before any session is started).
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    request_id: int | str | None = Field(
        ..., alias="requestId", description="The request this elicitation is tied to."
    )


class ElicitationSchemaType(StrEnum):
    """
    Type discriminator for elicitation schemas.
    """

    object = "object"


class ElicitationPropertySchema6(BaseModel):
    """
    Custom or future elicitation property schema.

    Values beginning with `_` are reserved for implementation-specific
    extensions. Unknown values that do not begin with `_` are reserved for
    future ACP variants.

    Clients that do not understand this property schema type should preserve
    the raw schema when storing, replaying, proxying, or forwarding
    elicitation requests. They MUST NOT render it as a known input control.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    type: str = Field(
        ...,
        description="Custom or future elicitation property schema type.\n\nValues beginning with `_` are reserved for implementation-specific\nextensions. Unknown values that do not begin with `_` are reserved for\nfuture ACP variants.",
    )


class StringFormat(StrEnum):
    """
    String format types for string properties in elicitation schemas.
    """

    email = "email"
    uri = "uri"
    date = "date"
    date_time = "date-time"


class EnumOption(BaseModel):
    """
    A titled enum option with a const value, human-readable title, and optional description.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    const: str = Field(..., description="The constant value for this option.")
    title: str = Field(..., description="Human-readable title for this option.")
    description: str | None = Field(
        None,
        description="Human-readable description.\n\nOptional. Omitted and `null` are equivalent and mean no description is provided.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nOptional. Omitted and `null` are equivalent and mean no metadata.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class StringPropertySchema(BaseModel):
    """
    Schema for string properties in an elicitation form.

    When `enum` or `oneOf` is set, this represents a single-select enum
    with `"type": "string"`.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    title: str | None = Field(
        None,
        description="Optional title for the property.\n\nOptional. Omitted and `null` are equivalent and mean no title is provided.",
    )
    description: str | None = Field(
        None,
        description="Human-readable description.\n\nOptional. Omitted and `null` are equivalent and mean no description is provided.",
    )
    min_length: conint(ge=0) | None = Field(
        None,
        alias="minLength",
        description="Minimum string length.\n\nOptional. Omitted and `null` are equivalent and mean there is no minimum length constraint.",
    )
    max_length: conint(ge=0) | None = Field(
        None,
        alias="maxLength",
        description="Maximum string length.\n\nOptional. Omitted and `null` are equivalent and mean there is no maximum length constraint.",
    )
    pattern: str | None = Field(
        None,
        description="Pattern the string must match.\n\nOptional. Omitted and `null` are equivalent and mean there is no pattern constraint.",
    )
    format: StringFormat | None = Field(
        None,
        description="String format.\n\nOptional. Omitted and `null` are equivalent and mean there is no format constraint.",
    )
    default: str | None = Field(
        None,
        description="Default value.\n\nOptional. Omitted and `null` are equivalent and mean no default value is provided.",
    )
    enum: list[str] | None = Field(
        None,
        description="Enum values for untitled single-select enums.\nOptional. Omitted and `null` are equivalent and mean no untitled single-select choices are\ndeclared by `enum`.",
    )
    one_of: list[EnumOption] | None = Field(
        None,
        alias="oneOf",
        description="Titled enum options for titled single-select enums.\nOptional. Omitted and `null` are equivalent and mean no titled single-select choices are\ndeclared by `oneOf`.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nOptional. Omitted and `null` are equivalent and mean no metadata.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class NumberPropertySchema(BaseModel):
    """
    Schema for number (floating-point) properties in an elicitation form.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    title: str | None = Field(
        None,
        description="Optional title for the property.\n\nOptional. Omitted and `null` are equivalent and mean no title is provided.",
    )
    description: str | None = Field(
        None,
        description="Human-readable description.\n\nOptional. Omitted and `null` are equivalent and mean no description is provided.",
    )
    minimum: float | None = Field(
        None,
        description="Minimum value (inclusive).\n\nOptional. Omitted and `null` are equivalent and mean there is no inclusive lower bound.",
    )
    maximum: float | None = Field(
        None,
        description="Maximum value (inclusive).\n\nOptional. Omitted and `null` are equivalent and mean there is no inclusive upper bound.",
    )
    default: float | None = Field(
        None,
        description="Default value.\n\nOptional. Omitted and `null` are equivalent and mean no default value is provided.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nOptional. Omitted and `null` are equivalent and mean no metadata.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class IntegerPropertySchema(BaseModel):
    """
    Schema for integer properties in an elicitation form.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    title: str | None = Field(
        None,
        description="Optional title for the property.\n\nOptional. Omitted and `null` are equivalent and mean no title is provided.",
    )
    description: str | None = Field(
        None,
        description="Human-readable description.\n\nOptional. Omitted and `null` are equivalent and mean no description is provided.",
    )
    minimum: int | None = Field(
        None,
        description="Minimum value (inclusive).\n\nOptional. Omitted and `null` are equivalent and mean there is no inclusive lower bound.",
    )
    maximum: int | None = Field(
        None,
        description="Maximum value (inclusive).\n\nOptional. Omitted and `null` are equivalent and mean there is no inclusive upper bound.",
    )
    default: int | None = Field(
        None,
        description="Default value.\n\nOptional. Omitted and `null` are equivalent and mean no default value is provided.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nOptional. Omitted and `null` are equivalent and mean no metadata.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class BooleanPropertySchema(BaseModel):
    """
    Schema for boolean properties in an elicitation form.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    title: str | None = Field(
        None,
        description="Optional title for the property.\n\nOptional. Omitted and `null` are equivalent and mean no title is provided.",
    )
    description: str | None = Field(
        None,
        description="Human-readable description.\n\nOptional. Omitted and `null` are equivalent and mean no description is provided.",
    )
    default: bool | None = Field(
        None,
        description="Default value.\n\nOptional. Omitted and `null` are equivalent and mean no default value is provided.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nOptional. Omitted and `null` are equivalent and mean no metadata.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class MultiSelectItems2(BaseModel):
    """
    Custom or future typed multi-select items.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    type: str = Field(
        ...,
        description="Custom or future multi-select item type.\n\nValues beginning with `_` are reserved for implementation-specific\nextensions. Unknown values that do not begin with `_` are reserved for\nfuture ACP variants.",
    )


class StringMultiSelectItems(BaseModel):
    """
    String item schema for multi-select enum properties.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    enum: list[str] = Field(..., description="Allowed enum values.")
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nOptional. Omitted and `null` are equivalent and mean no metadata.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class TitledMultiSelectItems(BaseModel):
    """
    Items definition for titled multi-select enum properties.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    any_of: list[EnumOption] = Field(
        ..., alias="anyOf", description="Titled enum options."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nOptional. Omitted and `null` are equivalent and mean no metadata.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class ElicitationUrlMode1(ElicitationSessionScope):
    """
    Tied to a session, optionally to a specific tool call within that session.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    elicitation_id: str = Field(
        ...,
        alias="elicitationId",
        description="The unique identifier for this elicitation.",
    )
    url: AnyUrl = Field(..., description="The URL to direct the user to.")


class ElicitationUrlMode2(ElicitationRequestScope):
    """
    Tied to a specific JSON-RPC request outside of a session
    (e.g., during auth/configuration phases before any session is started).
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    elicitation_id: str = Field(
        ...,
        alias="elicitationId",
        description="The unique identifier for this elicitation.",
    )
    url: AnyUrl = Field(..., description="The URL to direct the user to.")


class ElicitationUrlMode(RootModel[ElicitationUrlMode1 | ElicitationUrlMode2]):
    root: ElicitationUrlMode1 | ElicitationUrlMode2 = Field(
        ...,
        description="URL-based elicitation mode where the client directs the user to a URL.",
    )


class PromptCapabilities(BaseModel):
    """
    Prompt capabilities supported by the agent in `session/prompt` requests.

    Baseline agent functionality requires support for [`ContentBlock::Text`]
    and [`ContentBlock::ResourceLink`] in prompt requests.

    Other variants must be explicitly opted in to.
    Capabilities for different types of content in prompt requests.

    Indicates which content types beyond the baseline (text and resource links)
    the agent can process.

    See protocol docs: [Prompt Capabilities](https://agentclientprotocol.com/protocol/initialization#prompt-capabilities)
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    image: bool | None = Field(
        False, description="Agent supports [`ContentBlock::Image`]."
    )
    audio: bool | None = Field(
        False, description="Agent supports [`ContentBlock::Audio`]."
    )
    embedded_context: bool | None = Field(
        False,
        alias="embeddedContext",
        description="Agent supports embedded context in `session/prompt` requests.\n\nWhen enabled, the Client is allowed to include [`ContentBlock::Resource`]\nin prompt requests for pieces of context that are referenced in the message.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class McpCapabilities(BaseModel):
    """
    MCP capabilities supported by the agent
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    http: bool | None = Field(False, description="Agent supports [`McpServer::Http`].")
    sse: bool | None = Field(False, description="Agent supports [`McpServer::Sse`].")
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class SessionListCapabilities(BaseModel):
    """
    Capabilities for the `session/list` method.

    Supplying `{}` means the agent supports listing sessions.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class SessionDeleteCapabilities(BaseModel):
    """
    Capabilities for the `session/delete` method.

    Supplying `{}` means the agent supports deleting sessions from `session/list`.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class SessionAdditionalDirectoriesCapabilities(BaseModel):
    """
    Capabilities for additional session directories support.

    Supplying `{}` means the agent supports the `additionalDirectories` field on
    supported session lifecycle requests. Agents that also support
    `session/list` may return `SessionInfo.additionalDirectories` to report the
    complete ordered additional-root list associated with a listed session.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class SessionResumeCapabilities(BaseModel):
    """
    Capabilities for the `session/resume` method.

    Supplying `{}` means the agent supports resuming sessions.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class SessionCloseCapabilities(BaseModel):
    """
    Capabilities for the `session/close` method.

    Supplying `{}` means the agent supports closing sessions.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class LogoutCapabilities(BaseModel):
    """
    Logout capabilities supported by the agent.

    Supplying `{}` means the agent supports the logout method.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class AuthMethodTerminal(BaseModel):
    """
    Terminal-based authentication method.

    The client runs the configured agent program as a separate interactive
    process for the user to authenticate via a TUI. Agents MUST advertise this
    method only when the client enabled its terminal authentication capability.
    A zero exit status signals success; any other termination signals failure.
    The client MUST NOT pass this method to `authenticate`.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    id: str = Field(
        ..., description="Unique identifier for this authentication method."
    )
    name: str = Field(
        ..., description="Human-readable name of the authentication method."
    )
    description: str | None = Field(
        None,
        description="Optional description providing more details about this authentication method.",
    )
    args: list[str] | None = Field(
        None,
        description="Additional arguments to append to the configured agent invocation for terminal auth.",
    )
    env: dict[str, str] | None = Field(
        None,
        description="Additional environment variables to set on the configured agent invocation for terminal auth.\nThese values override same-named variables in the base launch configuration.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class AuthMethodAgent(BaseModel):
    """
    Agent handles authentication itself through `authenticate`.

    This is the default authentication method type.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    id: str = Field(
        ..., description="Unique identifier for this authentication method."
    )
    name: str = Field(
        ..., description="Human-readable name of the authentication method."
    )
    description: str | None = Field(
        None,
        description="Optional description providing more details about this authentication method.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class Implementation(BaseModel):
    """
    Metadata about the implementation of the client or agent.
    Describes the name and version of an ACP implementation, with an optional
    title for UI representation.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    name: str = Field(
        ...,
        description="Intended for programmatic or logical use, but can be used as a display\nname fallback if title isn’t present.",
    )
    title: str | None = Field(
        None,
        description="Intended for UI and end-user contexts — optimized to be human-readable\nand easily understood.\n\nIf not provided, the name should be used for display.",
    )
    version: str = Field(
        ...,
        description='Version of the implementation. Can be displayed to the user or used\nfor debugging or metrics purposes. (e.g. "1.0.0").',
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class AuthenticateResponse(BaseModel):
    """
    Response to the `authenticate` method.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class LogoutResponse(BaseModel):
    """
    Response to the `logout` method.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class SessionMode(BaseModel):
    """
    A mode the agent can operate in.

    See protocol docs: [Session Modes](https://agentclientprotocol.com/protocol/session-modes)
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    id: str = Field(
        ...,
        description="Stable identifier used to refer to this protocol object in later messages.",
    )
    name: str = Field(
        ..., description="Human-readable name shown for this protocol object."
    )
    description: str | None = Field(
        None,
        description="Optional human-readable details shown with this protocol object.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class SessionConfigSelectOption(BaseModel):
    """
    A possible value for a session configuration option.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    value: str = Field(..., description="Unique identifier for this option value.")
    name: str = Field(..., description="Human-readable label for this option value.")
    description: str | None = Field(
        None, description="Optional description for this option value."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class SessionConfigBoolean(BaseModel):
    """
    A boolean on/off toggle session configuration option payload.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    current_value: bool = Field(
        ...,
        alias="currentValue",
        description="The current value of the boolean option.",
    )


class SessionInfo(BaseModel):
    """
    Information about a session returned by session/list
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    session_id: str = Field(
        ..., alias="sessionId", description="Unique identifier for the session"
    )
    cwd: str = Field(
        ...,
        description="The working directory for this session. Must be an absolute path.",
    )
    additional_directories: list[str] | None = Field(
        None,
        alias="additionalDirectories",
        description="Additional workspace roots reported for this session. Each path must be absolute.\n\nWhen present, this is the complete ordered additional-root list reported\nby the Agent. Omitted and empty values are equivalent: the response\nreports no additional roots.",
    )
    title: str | None = Field(None, description="Human-readable title for the session")
    updated_at: str | None = Field(
        None, alias="updatedAt", description="ISO 8601 timestamp of last activity"
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class DeleteSessionResponse(BaseModel):
    """
    Response from deleting a session.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class CloseSessionResponse(BaseModel):
    """
    Response from closing a session.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class SetSessionModeResponse(BaseModel):
    """
    Response to `session/set_mode` method.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class StopReason(StrEnum):
    """
    Reasons why an agent stops processing a prompt turn.

    See protocol docs: [Stop Reasons](https://agentclientprotocol.com/protocol/prompt-turn#stop-reasons)
    """

    end_turn = "end_turn"
    max_tokens = "max_tokens"
    max_turn_requests = "max_turn_requests"
    refusal = "refusal"
    cancelled = "cancelled"


class PlanEntryPriority(StrEnum):
    """
    Priority levels for plan entries.

    Used to indicate the relative importance or urgency of different
    tasks in the execution plan.
    See protocol docs: [Plan Entries](https://agentclientprotocol.com/protocol/agent-plan#plan-entries)
    """

    high = "high"
    medium = "medium"
    low = "low"


class PlanEntryStatus(StrEnum):
    """
    Status of a plan entry in the execution flow.

    Tracks the lifecycle of each task from planning through completion.
    See protocol docs: [Plan Entries](https://agentclientprotocol.com/protocol/agent-plan#plan-entries)
    """

    pending = "pending"
    in_progress = "in_progress"
    completed = "completed"


class UnstructuredCommandInput(BaseModel):
    """
    All text that was typed after the command name is provided as input.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    hint: str = Field(
        ..., description="A hint to display when the input hasn't been provided yet"
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class CurrentModeUpdate(BaseModel):
    """
    The current mode of the session has changed

    See protocol docs: [Session Modes](https://agentclientprotocol.com/protocol/session-modes)
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    current_mode_id: str = Field(
        ..., alias="currentModeId", description="The ID of the current mode"
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class SessionInfoUpdate(BaseModel):
    """
    Update to session metadata. All fields are optional to support partial updates.

    Agents send this notification to update session information like title or custom metadata.
    This allows clients to display dynamic session names and track session state changes.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    title: str | None = Field(
        None, description="Human-readable title for the session. Set to null to clear."
    )
    updated_at: str | None = Field(
        None,
        alias="updatedAt",
        description="ISO 8601 timestamp of last activity. Set to null to clear.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class Cost(BaseModel):
    """
    Cost information for a session.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    amount: float = Field(..., description="Total cumulative cost for session.")
    currency: str = Field(
        ..., description='ISO 4217 currency code (e.g., "USD", "EUR").'
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class UsageUpdate(BaseModel):
    """
    Context window and cost update for a session.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    used: conint(ge=0) = Field(..., description="Tokens currently in context.")
    size: conint(ge=0) = Field(..., description="Total context window size in tokens.")
    cost: Cost | None = Field(None, description="Cumulative session cost (optional).")
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class CompleteElicitationNotification(BaseModel):
    """
    Notification sent by the agent when a URL-based elicitation is complete.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    elicitation_id: str = Field(
        ...,
        alias="elicitationId",
        description="The ID of the elicitation that completed.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nOptional. Omitted and `null` are equivalent and mean no metadata.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class FileSystemCapabilities(BaseModel):
    """
    File system capabilities that a client may support.

    See protocol docs: [FileSystem](https://agentclientprotocol.com/protocol/initialization#filesystem)
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    read_text_file: bool | None = Field(
        False,
        alias="readTextFile",
        description="Whether the Client supports `fs/read_text_file` requests.",
    )
    write_text_file: bool | None = Field(
        False,
        alias="writeTextFile",
        description="Whether the Client supports `fs/write_text_file` requests.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class BooleanConfigOptionCapabilities(BaseModel):
    """
    Capabilities for boolean session configuration options.

    Supplying `{}` means the client supports boolean session configuration options.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class AuthCapabilities(BaseModel):
    """
    Authentication capabilities supported by the client.

    Advertised during initialization to inform the agent which authentication
    method types the client can handle. This governs opt-in types that require
    additional client-side support.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    terminal: bool | None = Field(
        False,
        description="Whether the client supports `terminal` authentication methods.\n\nThe client should set this to `true` only when it can reproduce the\nconfigured agent invocation in an interactive terminal. When `true`, the\nagent may include `terminal` entries in its authentication methods.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class ElicitationFormCapabilities(BaseModel):
    """
    Form-based elicitation capabilities.

    Supplying `{}` means the client supports form-based elicitation.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nOptional. Omitted and `null` are equivalent and mean no metadata.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class ElicitationUrlCapabilities(BaseModel):
    """
    URL-based elicitation capabilities.

    Supplying `{}` means the client supports URL-based elicitation.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nOptional. Omitted and `null` are equivalent and mean no metadata.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class AuthenticateRequest(BaseModel):
    """
    Request parameters for the authenticate method.

    Specifies which authentication method to use.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    method_id: str = Field(
        ...,
        alias="methodId",
        description="The ID of the authentication method to use.\nMust be one of the methods advertised in the initialize response.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class LogoutRequest(BaseModel):
    """
    Request parameters for the logout method.

    Terminates the current authenticated session.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class HttpHeader(BaseModel):
    """
    An HTTP header to set when making requests to the MCP server.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    name: str = Field(..., description="The name of the HTTP header.")
    value: str = Field(..., description="The value to set for the HTTP header.")
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class McpServerHttp(BaseModel):
    """
    HTTP transport configuration for MCP.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    name: str = Field(
        ..., description="Human-readable name identifying this MCP server."
    )
    url: str = Field(..., description="URL to the MCP server.")
    headers: list[HttpHeader] = Field(
        ..., description="HTTP headers to set when making requests to the MCP server."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class McpServerSse(BaseModel):
    """
    SSE transport configuration for MCP.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    name: str = Field(
        ..., description="Human-readable name identifying this MCP server."
    )
    url: str = Field(..., description="URL to the MCP server.")
    headers: list[HttpHeader] = Field(
        ..., description="HTTP headers to set when making requests to the MCP server."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class McpServerStdio(BaseModel):
    """
    Stdio transport configuration for MCP.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    name: str = Field(
        ..., description="Human-readable name identifying this MCP server."
    )
    command: str = Field(..., description="Absolute path to the MCP server executable.")
    args: list[str] = Field(
        ..., description="Command-line arguments to pass to the MCP server."
    )
    env: list[EnvVariable] = Field(
        ..., description="Environment variables to set when launching the MCP server."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class ListSessionsRequest(BaseModel):
    """
    Request parameters for listing existing sessions.

    Only available if the Agent supports the `sessionCapabilities.list` capability.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    cwd: str | None = Field(
        None,
        description="Filter sessions by working directory. Must be an absolute path.",
    )
    cursor: str | None = Field(
        None,
        description="Opaque cursor token from a previous response's nextCursor field for cursor-based pagination",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class DeleteSessionRequest(BaseModel):
    """
    Request parameters for deleting an existing session from `session/list`.

    Only available if the Agent supports the `sessionCapabilities.delete` capability.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    session_id: str = Field(
        ..., alias="sessionId", description="The ID of the session to delete."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class CloseSessionRequest(BaseModel):
    """
    Request parameters for closing an active session.

    If supported, the agent **must** cancel any ongoing work related to the session
    (treat it as if `session/cancel` was called) and then free up any resources
    associated with the session.

    Only available if the Agent supports the `sessionCapabilities.close` capability.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    session_id: str = Field(
        ..., alias="sessionId", description="The ID of the session to close."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class SetSessionModeRequest(BaseModel):
    """
    Request parameters for setting a session mode.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    session_id: str = Field(
        ..., alias="sessionId", description="The ID of the session to set the mode for."
    )
    mode_id: str = Field(..., alias="modeId", description="The ID of the mode to set.")
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class SetSessionConfigOptionRequest1(BaseModel):
    """
    A boolean value (`type: "boolean"`).
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    session_id: str = Field(
        ...,
        alias="sessionId",
        description="The ID of the session to set the configuration option for.",
    )
    config_id: str = Field(
        ..., alias="configId", description="The ID of the configuration option to set."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )
    value: bool = Field(..., description="The boolean value.")
    type: Literal["boolean"]


class SetSessionConfigOptionRequest2(BaseModel):
    """
    A [`SessionConfigValueId`] string value.

    This is the default when `type` is absent on the wire. Unknown `type`
    values with string payloads also gracefully deserialize into this
    variant.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    session_id: str = Field(
        ...,
        alias="sessionId",
        description="The ID of the session to set the configuration option for.",
    )
    config_id: str = Field(
        ..., alias="configId", description="The ID of the configuration option to set."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )
    value: str = Field(..., description="The value ID.")


class WriteTextFileResponse(BaseModel):
    """
    Response to `fs/write_text_file`
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class ReadTextFileResponse(BaseModel):
    """
    Response containing the contents of a text file.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    content: str = Field(..., description="Content payload returned by this response.")
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class RequestPermissionOutcome1(BaseModel):
    """
    The prompt turn was cancelled before the user responded.

    When a client sends a `session/cancel` notification to cancel an ongoing
    prompt turn, it MUST respond to all pending `session/request_permission`
    requests with this `Cancelled` outcome.

    See protocol docs: [Cancellation](https://agentclientprotocol.com/protocol/prompt-turn#cancellation)
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    outcome: Literal["cancelled"]


class SelectedPermissionOutcome(BaseModel):
    """
    The user selected one of the provided options.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    option_id: str = Field(
        ..., alias="optionId", description="The ID of the option the user selected."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class CreateTerminalResponse(BaseModel):
    """
    Response containing the ID of the created terminal.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    terminal_id: str = Field(
        ...,
        alias="terminalId",
        description="The unique identifier for the created terminal.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class TerminalExitStatus(BaseModel):
    """
    Exit status of a terminal command.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    exit_code: conint(ge=0) | None = Field(
        None,
        alias="exitCode",
        description="The process exit code (may be null if terminated by signal).",
    )
    signal: str | None = Field(
        None,
        description="The signal that terminated the process (may be null if exited normally).",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class ReleaseTerminalResponse(BaseModel):
    """
    Response to terminal/release method
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class WaitForTerminalExitResponse(BaseModel):
    """
    Response containing the exit status of a terminal command.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    exit_code: conint(ge=0) | None = Field(
        None,
        alias="exitCode",
        description="The process exit code (may be null if terminated by signal).",
    )
    signal: str | None = Field(
        None,
        description="The signal that terminated the process (may be null if exited normally).",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class KillTerminalResponse(BaseModel):
    """
    Response to `terminal/kill` method
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class CreateElicitationResponse2(BaseModel):
    """
    The user declined the elicitation.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nOptional. Omitted and `null` are equivalent and mean no metadata.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )
    action: Literal["decline"]


class CreateElicitationResponse3(BaseModel):
    """
    The elicitation was cancelled.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nOptional. Omitted and `null` are equivalent and mean no metadata.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )
    action: Literal["cancel"]


class CreateElicitationResponse4(BaseModel):
    """
    Custom or future elicitation action.

    Values beginning with `_` are reserved for implementation-specific
    extensions. Unknown values that do not begin with `_` are reserved for
    future ACP variants.

    Agents that do not understand this action should preserve the raw
    payload when storing, replaying, proxying, or forwarding elicitation
    responses. They MUST NOT treat it as a known elicitation action.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nOptional. Omitted and `null` are equivalent and mean no metadata.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )
    action: str = Field(
        ...,
        description="Custom or future elicitation action.\n\nValues beginning with `_` are reserved for implementation-specific\nextensions. Unknown values that do not begin with `_` are reserved for\nfuture ACP variants.",
    )


class ElicitationContentValue(RootModel[str | int | float | bool | list[str]]):
    root: str | int | float | bool | list[str] = Field(
        ..., description="Allowed wire representations for [`ElicitationContentValue`]."
    )


class ElicitationAcceptAction(BaseModel):
    """
    The user accepted the elicitation and provided content.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    content: dict[str, Any] | None = Field(
        None,
        description="The user-provided content, if any, as an object matching the requested schema.",
    )


class CancelNotification(BaseModel):
    """
    Notification to cancel ongoing operations for a session.

    See protocol docs: [Cancellation](https://agentclientprotocol.com/protocol/prompt-turn#cancellation)
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    session_id: str = Field(
        ...,
        alias="sessionId",
        description="The ID of the session to cancel operations for.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class CancelRequestNotification(BaseModel):
    """
    Notification to cancel an ongoing request.

    See protocol docs: [Cancellation](https://agentclientprotocol.com/protocol/cancellation)
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    request_id: int | str | None = Field(
        ..., alias="requestId", description="The ID of the request to cancel."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class AgentClientProtocol7(BaseModel):
    """
    A message (request, response, or notification) with `"jsonrpc": "2.0"` specified as
    [required by JSON-RPC 2.0 Specification][1].

    [1]: https://www.jsonrpc.org/specification#compatibility
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    jsonrpc: Jsonrpc
    method: str = Field(..., description="The notification method name.")
    params: CancelRequestNotification | None = Field(
        None, description="Method-specific notification parameters."
    )


class WriteTextFileRequest(BaseModel):
    """
    Request to write content to a text file.

    Only available if the client supports the `fs.writeTextFile` capability.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    session_id: str = Field(
        ..., alias="sessionId", description="The session ID for this request."
    )
    path: str = Field(..., description="Absolute path to the file to write.")
    content: str = Field(..., description="The text content to write to the file.")
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class ToolCallContent2(Diff):
    """
    File modification shown as a diff.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    type: Literal["diff"]


class ToolCallContent3(Terminal):
    """
    Embed a terminal created with `terminal/create` by its id.

    The terminal must be added before calling `terminal/release`.

    See protocol docs: [Terminal](https://agentclientprotocol.com/protocol/terminals)
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    type: Literal["terminal"]


class Annotations(BaseModel):
    """
    Optional annotations for the client. The client can use annotations to inform how objects are used or displayed
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    audience: list[Role] | None = Field(
        None,
        description="Intended recipients for this content, such as the user or assistant.",
    )
    last_modified: str | None = Field(
        None,
        alias="lastModified",
        description="Timestamp indicating when the underlying resource was last modified.",
    )
    priority: float | None = Field(
        None,
        description="Relative importance of this content when clients choose what to surface.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class TextContent(BaseModel):
    """
    Text provided to or from an LLM.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    annotations: Annotations | None = Field(
        None,
        description="Optional annotations that help clients decide how to display or route this content.",
    )
    text: str = Field(..., description="Text payload carried by this content block.")
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class ImageContent(BaseModel):
    """
    An image provided to or from an LLM.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    annotations: Annotations | None = Field(
        None,
        description="Optional annotations that help clients decide how to display or route this content.",
    )
    data: str = Field(..., description="Base64-encoded media payload.")
    mime_type: str = Field(
        ...,
        alias="mimeType",
        description="MIME type describing the encoded media payload.",
    )
    uri: str | None = Field(
        None, description="URI associated with this resource or media payload."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class AudioContent(BaseModel):
    """
    Audio provided to or from an LLM.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    annotations: Annotations | None = Field(
        None,
        description="Optional annotations that help clients decide how to display or route this content.",
    )
    data: str = Field(..., description="Base64-encoded media payload.")
    mime_type: str = Field(
        ...,
        alias="mimeType",
        description="MIME type describing the encoded media payload.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class ResourceLink(BaseModel):
    """
    A resource that the server is capable of reading, included in a prompt or tool call result.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    annotations: Annotations | None = Field(
        None,
        description="Optional annotations that help clients decide how to display or route this content.",
    )
    description: str | None = Field(
        None,
        description="Optional human-readable details shown with this protocol object.",
    )
    mime_type: str | None = Field(
        None,
        alias="mimeType",
        description="MIME type describing the encoded media payload.",
    )
    name: str = Field(
        ..., description="Human-readable name shown for this protocol object."
    )
    size: int | None = Field(
        None, description="Optional size of the linked resource in bytes, if known."
    )
    title: str | None = Field(
        None, description="Optional display title for end-user UI."
    )
    uri: str = Field(
        ..., description="URI associated with this resource or media payload."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class EmbeddedResource(BaseModel):
    """
    The contents of a resource, embedded into a prompt or tool call result.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    annotations: Annotations | None = Field(
        None,
        description="Optional annotations that help clients decide how to display or route this content.",
    )
    resource: TextResourceContents | BlobResourceContents = Field(
        ..., description="Embedded resource payload, either text or binary data."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class PermissionOption(BaseModel):
    """
    An option presented to the user when requesting permission.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    option_id: str = Field(
        ...,
        alias="optionId",
        description="Unique identifier for this permission option.",
    )
    name: str = Field(..., description="Human-readable label to display to the user.")
    kind: PermissionOptionKind = Field(
        ..., description="Hint about the nature of this permission option."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class CreateTerminalRequest(BaseModel):
    """
    Request to create a new terminal and execute a command.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    session_id: str = Field(
        ..., alias="sessionId", description="The session ID for this request."
    )
    command: str = Field(..., description="The command to execute.")
    args: list[str] | None = Field(None, description="Array of command arguments.")
    env: list[EnvVariable] | None = Field(
        None, description="Environment variables for the command."
    )
    cwd: str | None = Field(
        None, description="Working directory for the command. Must be an absolute path."
    )
    output_byte_limit: conint(ge=0) | None = Field(
        None,
        alias="outputByteLimit",
        description="Maximum number of output bytes to retain.\n\nWhen the limit is exceeded, the Client truncates from the beginning of the output\nto stay within the limit.\n\nThe Client MUST ensure truncation happens at a character boundary to maintain valid\nstring output, even if this means the retained output is slightly less than the\nspecified limit.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class CreateElicitationRequest21(ElicitationSessionScope):
    """
    Tied to a session, optionally to a specific tool call within that session.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    elicitation_id: str = Field(
        ...,
        alias="elicitationId",
        description="The unique identifier for this elicitation.",
    )
    url: AnyUrl = Field(..., description="The URL to direct the user to.")


class CreateElicitationRequest22(ElicitationRequestScope):
    """
    Tied to a specific JSON-RPC request outside of a session
    (e.g., during auth/configuration phases before any session is started).
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    elicitation_id: str = Field(
        ...,
        alias="elicitationId",
        description="The unique identifier for this elicitation.",
    )
    url: AnyUrl = Field(..., description="The URL to direct the user to.")


class CreateElicitationRequest24(
    CreateElicitationRequest21, CreateElicitationRequest23
):
    """
    URL-based elicitation where the client directs the user to a URL.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    message: str = Field(
        ..., description="A human-readable message describing what input is needed."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nOptional. Omitted and `null` are equivalent and mean no metadata.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )
    mode: Literal["url"]


class CreateElicitationRequest25(
    CreateElicitationRequest22, CreateElicitationRequest23
):
    """
    URL-based elicitation where the client directs the user to a URL.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    message: str = Field(
        ..., description="A human-readable message describing what input is needed."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nOptional. Omitted and `null` are equivalent and mean no metadata.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )
    mode: Literal["url"]


class CreateElicitationRequest3(ElicitationSessionScope):
    """
    Tied to a session, optionally to a specific tool call within that session.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    message: str = Field(
        ..., description="A human-readable message describing what input is needed."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nOptional. Omitted and `null` are equivalent and mean no metadata.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )
    mode: str = Field(
        ...,
        description="Custom or future elicitation mode.\n\nValues beginning with `_` are reserved for implementation-specific\nextensions. Unknown values that do not begin with `_` are reserved for\nfuture ACP variants.",
    )


class CreateElicitationRequest4(ElicitationRequestScope):
    """
    Tied to a specific JSON-RPC request outside of a session
    (e.g., during auth/configuration phases before any session is started).
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    message: str = Field(
        ..., description="A human-readable message describing what input is needed."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nOptional. Omitted and `null` are equivalent and mean no metadata.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )
    mode: str = Field(
        ...,
        description="Custom or future elicitation mode.\n\nValues beginning with `_` are reserved for implementation-specific\nextensions. Unknown values that do not begin with `_` are reserved for\nfuture ACP variants.",
    )


class ElicitationPropertySchema1(StringPropertySchema):
    """
    String property (or single-select enum when `enum`/`oneOf` is set).
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    type: Literal["string"]


class ElicitationPropertySchema2(NumberPropertySchema):
    """
    Number (floating-point) property.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    type: Literal["number"]


class ElicitationPropertySchema3(IntegerPropertySchema):
    """
    Integer property.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    type: Literal["integer"]


class ElicitationPropertySchema4(BooleanPropertySchema):
    """
    Boolean property.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    type: Literal["boolean"]


class MultiSelectItems1(StringMultiSelectItems):
    """
    Multi-select string items with plain string values.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    type: Literal["string"]


class MultiSelectPropertySchema(BaseModel):
    """
    Schema for multi-select (array) properties in an elicitation form.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    title: str | None = Field(
        None,
        description="Optional title for the property.\n\nOptional. Omitted and `null` are equivalent and mean no title is provided.",
    )
    description: str | None = Field(
        None,
        description="Human-readable description.\n\nOptional. Omitted and `null` are equivalent and mean no description is provided.",
    )
    min_items: conint(ge=0) | None = Field(
        None,
        alias="minItems",
        description="Minimum number of items to select.\n\nOptional. Omitted and `null` are equivalent and mean there is no minimum selection count.",
    )
    max_items: conint(ge=0) | None = Field(
        None,
        alias="maxItems",
        description="Maximum number of items to select.\n\nOptional. Omitted and `null` are equivalent and mean there is no maximum selection count.",
    )
    items: MultiSelectItems1 | MultiSelectItems2 | TitledMultiSelectItems = Field(
        ..., description="The items definition describing allowed values."
    )
    default: list[str] | None = Field(
        None,
        description="Default selected values.\n\nOptional. Omitted and `null` are equivalent and mean no default selections are provided.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nOptional. Omitted and `null` are equivalent and mean no metadata.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class SessionCapabilities(BaseModel):
    """
    Session capabilities supported by the agent.

    As a baseline, all Agents **MUST** support `session/new`, `session/prompt`, `session/cancel`, and `session/update`.

    Optionally, they **MAY** support other session methods and notifications by specifying additional capabilities.

    Note: `session/load` is still handled by the top-level `load_session` capability. This will be unified in future versions of the protocol.

    See protocol docs: [Session Capabilities](https://agentclientprotocol.com/protocol/initialization#session-capabilities)
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    list: SessionListCapabilities | None = Field(
        None,
        description="Whether the agent supports `session/list`.\n\nOptional. Omitted or `null` both mean the agent does not advertise support.\nSupplying `{}` means the agent supports listing sessions.",
    )
    delete: SessionDeleteCapabilities | None = Field(
        None,
        description="Whether the agent supports `session/delete`.\n\nOptional. Omitted or `null` both mean the agent does not advertise support.\nSupplying `{}` means the agent supports deleting sessions from `session/list`.",
    )
    additional_directories: SessionAdditionalDirectoriesCapabilities | None = Field(
        None,
        alias="additionalDirectories",
        description="Whether the agent supports `additionalDirectories` on supported session lifecycle requests.\n\nOptional. Omitted or `null` both mean the agent does not advertise support.\nSupplying `{}` means the agent supports `additionalDirectories` on\nsupported session lifecycle requests.\n\nAgents that also support `session/list` may return\n`SessionInfo.additionalDirectories` to report the complete ordered\nadditional-root list associated with a listed session.",
    )
    resume: SessionResumeCapabilities | None = Field(
        None,
        description="Whether the agent supports `session/resume`.\n\nOptional. Omitted or `null` both mean the agent does not advertise support.\nSupplying `{}` means the agent supports resuming sessions.",
    )
    close: SessionCloseCapabilities | None = Field(
        None,
        description="Whether the agent supports `session/close`.\n\nOptional. Omitted or `null` both mean the agent does not advertise support.\nSupplying `{}` means the agent supports closing sessions.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class AgentAuthCapabilities(BaseModel):
    """
    Authentication-related capabilities supported by the agent.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    logout: LogoutCapabilities | None = Field(
        None,
        description="Whether the agent supports the logout method.\n\nOptional. Omitted or `null` both mean the agent does not advertise support.\nSupplying `{}` means the agent supports the logout method.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class AuthMethod1(AuthMethodTerminal):
    """
    Client runs the configured agent program as a separate interactive
    process, without passing this method to `authenticate`.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    type: Literal["terminal"]


class SessionModeState(BaseModel):
    """
    The set of modes and the one currently active.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    current_mode_id: str = Field(
        ..., alias="currentModeId", description="The current mode the Agent is in."
    )
    available_modes: list[SessionMode] = Field(
        ...,
        alias="availableModes",
        description="The set of modes that the Agent can operate in",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class SessionConfigOption2(SessionConfigBoolean):
    """
    Boolean on/off toggle.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    id: str = Field(..., description="Unique identifier for the configuration option.")
    name: str = Field(..., description="Human-readable label for the option.")
    description: str | None = Field(
        None, description="Optional description for the Client to display to the user."
    )
    category: (
        Literal["mode"]
        | Literal["model"]
        | Literal["model_config"]
        | Literal["thought_level"]
        | str
        | None
    ) = Field(None, description="Optional semantic category for this option (UX only).")
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )
    type: Literal["boolean"]


class SessionConfigSelectGroup(BaseModel):
    """
    A group of possible values for a session configuration option.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    group: str = Field(..., description="Unique identifier for this group.")
    name: str = Field(..., description="Human-readable label for this group.")
    options: list[SessionConfigSelectOption] = Field(
        ..., description="The set of option values in this group."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class ListSessionsResponse(BaseModel):
    """
    Response from listing sessions.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    sessions: list[SessionInfo] = Field(
        ..., description="Array of session information objects"
    )
    next_cursor: str | None = Field(
        None,
        alias="nextCursor",
        description="Opaque cursor token. If present, pass this in the next request's cursor parameter\nto fetch the next page. If absent, there are no more results.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class PromptResponse(BaseModel):
    """
    Response from processing a user prompt.

    See protocol docs: [Check for Completion](https://agentclientprotocol.com/protocol/prompt-turn#4-check-for-completion)
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    stop_reason: StopReason = Field(
        ...,
        alias="stopReason",
        description="Indicates why the agent stopped processing the turn.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class Error(BaseModel):
    """
    JSON-RPC error object.

    Represents an error that occurred during method execution, following the
    JSON-RPC 2.0 error object specification with optional additional data.

    See protocol docs: [JSON-RPC Error Object](https://www.jsonrpc.org/specification#error_object)
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    code: (
        Literal[-32700]
        | Literal[-32600]
        | Literal[-32601]
        | Literal[-32602]
        | Literal[-32603]
        | Literal[-32800]
        | Literal[-32000]
        | Literal[-32002]
        | int
    ) = Field(
        ...,
        description="A number indicating the error type that occurred.\nThis must be an integer as defined in the JSON-RPC specification.",
    )
    message: str = Field(
        ...,
        description="A string providing a short description of the error.\nThe message should be limited to a concise single sentence.",
    )
    data: Any | None = Field(
        None,
        description="Optional primitive or structured value that contains additional information about the error.\nThis may include debugging information or context-specific details.",
    )


class SessionUpdate8(CurrentModeUpdate):
    """
    The current mode of the session has changed

    See protocol docs: [Session Modes](https://agentclientprotocol.com/protocol/session-modes)
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    session_update: Literal["current_mode_update"] = Field(..., alias="sessionUpdate")


class SessionUpdate10(SessionInfoUpdate):
    """
    Session metadata has been updated (title, timestamps, custom metadata)
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    session_update: Literal["session_info_update"] = Field(..., alias="sessionUpdate")


class SessionUpdate11(UsageUpdate):
    """
    Context window and cost update for the session.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    session_update: Literal["usage_update"] = Field(..., alias="sessionUpdate")


class PlanEntry(BaseModel):
    """
    A single entry in the execution plan.

    Represents a task or goal that the assistant intends to accomplish
    as part of fulfilling the user's request.
    See protocol docs: [Plan Entries](https://agentclientprotocol.com/protocol/agent-plan#plan-entries)
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    content: str = Field(
        ...,
        description="Human-readable description of what this task aims to accomplish.",
    )
    priority: PlanEntryPriority = Field(
        ...,
        description="The relative importance of this task.\nUsed to indicate which tasks are most critical to the overall goal.",
    )
    status: PlanEntryStatus = Field(
        ..., description="Current execution status of this task."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class Plan(BaseModel):
    """
    An execution plan for accomplishing complex tasks.

    Plans consist of multiple entries representing individual tasks or goals.
    Agents report plans to clients to provide visibility into their execution strategy.
    Plans can evolve during execution as the agent discovers new requirements or completes tasks.

    See protocol docs: [Agent Plan](https://agentclientprotocol.com/protocol/agent-plan)
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    entries: list[PlanEntry] = Field(
        ...,
        description="The list of tasks to be accomplished.\n\nWhen updating a plan, the agent must send a complete list of all entries\nwith their current status. The client replaces the entire plan with each update.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class AvailableCommandInput(RootModel[UnstructuredCommandInput]):
    root: UnstructuredCommandInput = Field(
        ..., description="The input specification for a command."
    )


class SessionConfigOptionsCapabilities(BaseModel):
    """
    Session configuration option capabilities supported by the client.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    boolean: BooleanConfigOptionCapabilities | None = Field(
        None,
        description='Whether the client supports boolean session configuration options.\n\nOptional. Omitted or `null` both mean the client does not advertise support.\nSupplying `{}` means agents may include `type: "boolean"` entries in\n`configOptions`, and the client may send `session/set_config_option`\nrequests with `type: "boolean"` and a boolean `value`.',
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class ElicitationCapabilities(BaseModel):
    """
    Elicitation capabilities supported by the client.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    form: ElicitationFormCapabilities | None = Field(
        None,
        description="Whether the client supports form-based elicitation.\n\nOptional. Omitted and `null` are equivalent and mean form support is not advertised.\nSupplying `{}` explicitly advertises form support.",
    )
    url: ElicitationUrlCapabilities | None = Field(
        None,
        description="Whether the client supports URL-based elicitation.\n\nOptional. Omitted or `null` both mean the client does not advertise support.\nSupplying `{}` means the client supports URL-based elicitation.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nOptional. Omitted and `null` are equivalent and mean no metadata.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class McpServer1(McpServerHttp):
    """
    HTTP transport configuration

    Only available when the Agent capabilities indicate `mcp_capabilities.http` is `true`.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    type: Literal["http"]


class McpServer2(McpServerSse):
    """
    SSE transport configuration

    Only available when the Agent capabilities indicate `mcp_capabilities.sse` is `true`.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    type: Literal["sse"]


class LoadSessionRequest(BaseModel):
    """
    Request parameters for loading an existing session.

    Only available if the Agent supports the `loadSession` capability.

    See protocol docs: [Loading Sessions](https://agentclientprotocol.com/protocol/session-setup#loading-sessions)
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    mcp_servers: list[McpServer1 | McpServer2 | McpServerStdio] = Field(
        ...,
        alias="mcpServers",
        description="List of MCP servers to connect to for this session.",
    )
    cwd: str = Field(
        ...,
        description="The working directory for this session. Must be an absolute path.",
    )
    additional_directories: list[str] | None = Field(
        None,
        alias="additionalDirectories",
        description="Additional workspace roots to activate for this session. Each path must be absolute.\n\nWhen omitted or empty, no additional roots are activated. When non-empty,\nthis is the complete resulting additional-root list for the loaded\nsession. It may differ from any previously used or reported list as long as\nthe request `cwd` matches the session's `cwd`.",
    )
    session_id: str = Field(
        ..., alias="sessionId", description="The ID of the session to load."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class ResumeSessionRequest(BaseModel):
    """
    Request parameters for resuming an existing session.

    Resumes an existing session without returning previous messages (unlike `session/load`).
    This is useful for agents that can resume sessions but don't implement full session loading.

    Only available if the Agent supports the `sessionCapabilities.resume` capability.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    session_id: str = Field(
        ..., alias="sessionId", description="The ID of the session to resume."
    )
    cwd: str = Field(
        ...,
        description="The working directory for this session. Must be an absolute path.",
    )
    additional_directories: list[str] | None = Field(
        None,
        alias="additionalDirectories",
        description="Additional workspace roots to activate for this session. Each path must be absolute.\n\nWhen omitted or empty, no additional roots are activated. When non-empty,\nthis is the complete resulting additional-root list for the resumed\nsession. It may differ from any previously used or reported list as long as\nthe request `cwd` matches the session's `cwd`.",
    )
    mcp_servers: list[McpServer1 | McpServer2 | McpServerStdio] | None = Field(
        None,
        alias="mcpServers",
        description="List of MCP servers to connect to for this session.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class ClientResponse2(BaseModel):
    """
    A failed JSON-RPC response.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    id: int | str | None = Field(
        ..., description="The id of the request this response answers."
    )
    error: Error = Field(..., description="Method-specific error data.")


class RequestPermissionOutcome2(SelectedPermissionOutcome):
    """
    The user selected one of the provided options.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    outcome: Literal["selected"]


class TerminalOutputResponse(BaseModel):
    """
    Response containing the terminal output and exit status.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    output: str = Field(..., description="The terminal output captured so far.")
    truncated: bool = Field(
        ..., description="Whether the output was truncated due to byte limits."
    )
    exit_status: TerminalExitStatus | None = Field(
        None,
        alias="exitStatus",
        description="Exit status if the command has completed.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class CreateElicitationResponse1(ElicitationAcceptAction):
    """
    The user accepted and provided content.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nOptional. Omitted and `null` are equivalent and mean no metadata.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )
    action: Literal["accept"]


class ClientNotification(BaseModel):
    """
    A JSON-RPC notification object.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    method: str = Field(..., description="The notification method name.")
    params: CancelNotification | Any | None = Field(
        None, description="Method-specific notification parameters."
    )


class AgentClientProtocol22(BaseModel):
    """
    A failed JSON-RPC response.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    id: int | str | None = Field(
        ..., description="The id of the request this response answers."
    )
    error: Error = Field(..., description="Method-specific error data.")


class AgentClientProtocol25(AgentClientProtocol22, AgentClientProtocol23):
    """
    A message (request, response, or notification) with `"jsonrpc": "2.0"` specified as
    [required by JSON-RPC 2.0 Specification][1].

    [1]: https://www.jsonrpc.org/specification#compatibility
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    jsonrpc: Jsonrpc


class AgentClientProtocol52(BaseModel):
    """
    A failed JSON-RPC response.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    id: int | str | None = Field(
        ..., description="The id of the request this response answers."
    )
    error: Error = Field(..., description="Method-specific error data.")


class AgentClientProtocol55(AgentClientProtocol52, AgentClientProtocol53):
    """
    A message (request, response, or notification) with `"jsonrpc": "2.0"` specified as
    [required by JSON-RPC 2.0 Specification][1].

    [1]: https://www.jsonrpc.org/specification#compatibility
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    jsonrpc: Jsonrpc


class AgentClientProtocol6(ClientNotification):
    """
    A message (request, response, or notification) with `"jsonrpc": "2.0"` specified as
    [required by JSON-RPC 2.0 Specification][1].

    [1]: https://www.jsonrpc.org/specification#compatibility
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    jsonrpc: Jsonrpc


class ContentBlock1(TextContent):
    """
    Text content. May be plain text or formatted with Markdown.

    All agents MUST support text content blocks in prompts.
    Clients SHOULD render this text as Markdown.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    type: Literal["text"]


class ContentBlock2(ImageContent):
    """
    Images for visual context or analysis.

    Requires the `image` prompt capability when included in prompts.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    type: Literal["image"]


class ContentBlock3(AudioContent):
    """
    Audio data for transcription or analysis.

    Requires the `audio` prompt capability when included in prompts.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    type: Literal["audio"]


class ContentBlock4(ResourceLink):
    """
    References to resources that the agent can access.

    All agents MUST support resource links in prompts.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    type: Literal["resource_link"]


class ContentBlock5(EmbeddedResource):
    """
    Complete resource contents embedded directly in the message.

    Preferred for including context as it avoids extra round-trips.

    Requires the `embeddedContext` prompt capability when included in prompts.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    type: Literal["resource"]


class Content(BaseModel):
    """
    Standard content block (text, images, resources).
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    content: (
        ContentBlock1 | ContentBlock2 | ContentBlock3 | ContentBlock4 | ContentBlock5
    ) = Field(..., description="The actual content block.", discriminator="type")
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class ElicitationPropertySchema5(MultiSelectPropertySchema):
    """
    Multi-select array property.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    type: Literal["array"]


class AgentResponse2(BaseModel):
    """
    A failed JSON-RPC response.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    id: int | str | None = Field(
        ..., description="The id of the request this response answers."
    )
    error: Error = Field(..., description="Method-specific error data.")


class AgentCapabilities(BaseModel):
    """
    Capabilities supported by the agent.

    Advertised during initialization to inform the client about
    available features and content types.

    See protocol docs: [Agent Capabilities](https://agentclientprotocol.com/protocol/initialization#agent-capabilities)
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    load_session: bool | None = Field(
        False,
        alias="loadSession",
        description="Whether the agent supports `session/load`.",
    )
    prompt_capabilities: PromptCapabilities | None = Field(
        {"image": False, "audio": False, "embeddedContext": False},
        alias="promptCapabilities",
        description="Prompt capabilities supported by the agent.",
        validate_default=True,
    )
    mcp_capabilities: McpCapabilities | None = Field(
        {"http": False, "sse": False},
        alias="mcpCapabilities",
        description="MCP capabilities supported by the agent.",
        validate_default=True,
    )
    session_capabilities: SessionCapabilities | None = Field(
        {},
        alias="sessionCapabilities",
        description="Session lifecycle and prompt capabilities advertised by the agent.",
        validate_default=True,
    )
    auth: AgentAuthCapabilities | None = Field(
        {},
        description="Authentication-related capabilities supported by the agent.",
        validate_default=True,
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class SessionConfigSelect(BaseModel):
    """
    A single-value selector (dropdown) session configuration option payload.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    current_value: str = Field(
        ..., alias="currentValue", description="The currently selected value."
    )
    options: list[SessionConfigSelectOption] | list[SessionConfigSelectGroup] = Field(
        ..., description="The set of selectable options."
    )


class SessionUpdate6(Plan):
    """
    The agent's execution plan for complex tasks.
    See protocol docs: [Agent Plan](https://agentclientprotocol.com/protocol/agent-plan)
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    session_update: Literal["plan"] = Field(..., alias="sessionUpdate")


class ContentChunk(BaseModel):
    """
    A streamed item of content
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    content: (
        ContentBlock1 | ContentBlock2 | ContentBlock3 | ContentBlock4 | ContentBlock5
    ) = Field(..., description="A single item of content", discriminator="type")
    message_id: str | None = Field(
        None,
        alias="messageId",
        description="A unique identifier for the message this chunk belongs to.\n\nAll chunks belonging to the same message share the same `messageId`.\nA change in `messageId` indicates a new message has started.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class AvailableCommand(BaseModel):
    """
    Information about a command.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    name: str = Field(
        ..., description="Command name (e.g., `create_plan`, `research_codebase`)."
    )
    description: str = Field(
        ..., description="Human-readable description of what the command does."
    )
    input: AvailableCommandInput | None = Field(
        None, description="Input for the command if required"
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class AvailableCommandsUpdate(BaseModel):
    """
    Available commands are ready or have changed
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    available_commands: list[AvailableCommand] = Field(
        ..., alias="availableCommands", description="Commands the agent can execute"
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class ClientSessionCapabilities(BaseModel):
    """
    Session-related capabilities supported by the client.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    config_options: SessionConfigOptionsCapabilities | None = Field(
        None,
        alias="configOptions",
        description="Config option capabilities supported by the client.\n\nOmitted or `null` both mean the client does not advertise support for any\nconfig option extensions.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class NewSessionRequest(BaseModel):
    """
    Request parameters for creating a new session.

    See protocol docs: [Creating a Session](https://agentclientprotocol.com/protocol/session-setup#creating-a-session)
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    cwd: str = Field(
        ...,
        description="The working directory for this session. Must be an absolute path.",
    )
    additional_directories: list[str] | None = Field(
        None,
        alias="additionalDirectories",
        description="Additional workspace roots for this session. Each path must be absolute.\n\nThese expand the session's filesystem scope without changing `cwd`, which\nremains the base for relative paths. When omitted or empty, no\nadditional roots are activated for the new session.",
    )
    mcp_servers: list[McpServer1 | McpServer2 | McpServerStdio] = Field(
        ...,
        alias="mcpServers",
        description="List of MCP (Model Context Protocol) servers the agent should connect to.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class PromptRequest(BaseModel):
    """
    Request parameters for sending a user prompt to the agent.

    Contains the user's message and any additional context.

    See protocol docs: [User Message](https://agentclientprotocol.com/protocol/prompt-turn#1-user-message)
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    session_id: str = Field(
        ...,
        alias="sessionId",
        description="The ID of the session to send this user message to",
    )
    prompt: list[
        Annotated[
            ContentBlock1
            | ContentBlock2
            | ContentBlock3
            | ContentBlock4
            | ContentBlock5,
            Field(discriminator="type"),
        ]
    ] = Field(
        ...,
        description="The blocks of content that compose the user's message.\n\nAs a baseline, the Agent MUST support [`ContentBlock::Text`] and [`ContentBlock::ResourceLink`],\nwhile other variants are optionally enabled via [`PromptCapabilities`].\n\nThe Client MUST adapt its interface according to [`PromptCapabilities`].\n\nThe client MAY include referenced pieces of context as either\n[`ContentBlock::Resource`] or [`ContentBlock::ResourceLink`].\n\nWhen available, [`ContentBlock::Resource`] is preferred\nas it avoids extra round-trips and allows the message to include\npieces of context from sources the agent may not have access to.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class RequestPermissionResponse(BaseModel):
    """
    Response to a permission request.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    outcome: RequestPermissionOutcome1 | RequestPermissionOutcome2 = Field(
        ...,
        description="The user's decision on the permission request.",
        discriminator="outcome",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class AgentClientProtocol51(BaseModel):
    """
    A successful JSON-RPC response.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    id: int | str | None = Field(
        ..., description="The id of the request this response answers."
    )
    result: (
        WriteTextFileResponse
        | ReadTextFileResponse
        | RequestPermissionResponse
        | CreateTerminalResponse
        | TerminalOutputResponse
        | ReleaseTerminalResponse
        | WaitForTerminalExitResponse
        | KillTerminalResponse
        | CreateElicitationResponse1
        | CreateElicitationResponse2
        | CreateElicitationResponse3
        | CreateElicitationResponse4
        | Any
    ) = Field(..., description="Method-specific response data.")


class AgentClientProtocol54(AgentClientProtocol51, AgentClientProtocol53):
    """
    A message (request, response, or notification) with `"jsonrpc": "2.0"` specified as
    [required by JSON-RPC 2.0 Specification][1].

    [1]: https://www.jsonrpc.org/specification#compatibility
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    jsonrpc: Jsonrpc


class ToolCallContent1(Content):
    """
    Standard content block (text, images, resources).
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    type: Literal["content"]


class ElicitationSchema(BaseModel):
    """
    Type-safe elicitation schema for requesting structured user input.

    This represents a JSON Schema object with primitive-typed properties,
    as required by the elicitation specification.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    type: ElicitationSchemaType | None = Field(
        "object", description='Type discriminator. Always `"object"`.'
    )
    title: str | None = Field(
        None,
        description="Optional title for the schema.\n\nOptional. Omitted and `null` are equivalent and mean no title is provided.",
    )
    properties: (
        dict[
            str,
            ElicitationPropertySchema1
            | ElicitationPropertySchema2
            | ElicitationPropertySchema3
            | ElicitationPropertySchema4
            | ElicitationPropertySchema5
            | ElicitationPropertySchema6,
        ]
        | None
    ) = Field(
        {},
        description="Property definitions (must be primitive types).",
        validate_default=True,
    )
    required: list[str] | None = Field(
        None,
        description="List of required property names.\n\nOptional. Omitted and `null` are equivalent and mean no property names are required.",
    )
    description: str | None = Field(
        None,
        description="Optional description of what this schema represents.\n\nOptional. Omitted and `null` are equivalent and mean no schema description is provided.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nOptional. Omitted and `null` are equivalent and mean no metadata.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class ElicitationFormMode1(ElicitationSessionScope):
    """
    Tied to a session, optionally to a specific tool call within that session.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    requested_schema: ElicitationSchema = Field(
        ...,
        alias="requestedSchema",
        description="A JSON Schema describing the form fields to present to the user.",
    )


class ElicitationFormMode2(ElicitationRequestScope):
    """
    Tied to a specific JSON-RPC request outside of a session
    (e.g., during auth/configuration phases before any session is started).
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    requested_schema: ElicitationSchema = Field(
        ...,
        alias="requestedSchema",
        description="A JSON Schema describing the form fields to present to the user.",
    )


class ElicitationFormMode(RootModel[ElicitationFormMode1 | ElicitationFormMode2]):
    root: ElicitationFormMode1 | ElicitationFormMode2 = Field(
        ...,
        description="Form-based elicitation mode where the client renders a form from the provided schema.",
    )


class InitializeResponse(BaseModel):
    """
    Response to the `initialize` method.

    Contains the negotiated protocol version and agent capabilities.

    See protocol docs: [Initialization](https://agentclientprotocol.com/protocol/initialization)
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    protocol_version: conint(ge=0, le=65535) = Field(
        ...,
        alias="protocolVersion",
        description="The protocol version the client specified if supported by the agent,\nor the latest protocol version supported by the agent.\n\nThe client should disconnect, if it doesn't support this version.",
    )
    agent_capabilities: AgentCapabilities | None = Field(
        {
            "loadSession": False,
            "promptCapabilities": {
                "image": False,
                "audio": False,
                "embeddedContext": False,
            },
            "mcpCapabilities": {"http": False, "sse": False},
            "sessionCapabilities": {},
            "auth": {},
        },
        alias="agentCapabilities",
        description="Capabilities supported by the agent.",
        validate_default=True,
    )
    auth_methods: list[AuthMethod1 | AuthMethodAgent] | None = Field(
        [],
        alias="authMethods",
        description="Authentication methods supported by the agent.",
        validate_default=True,
    )
    agent_info: Implementation | None = Field(
        None,
        alias="agentInfo",
        description="Information about the Agent name and version sent to the Client.\n\nNote: in future versions of the protocol, this will be required.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class SessionConfigOption1(SessionConfigSelect):
    """
    Single-value selector (dropdown).
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    id: str = Field(..., description="Unique identifier for the configuration option.")
    name: str = Field(..., description="Human-readable label for the option.")
    description: str | None = Field(
        None, description="Optional description for the Client to display to the user."
    )
    category: (
        Literal["mode"]
        | Literal["model"]
        | Literal["model_config"]
        | Literal["thought_level"]
        | str
        | None
    ) = Field(None, description="Optional semantic category for this option (UX only).")
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )
    type: Literal["select"]


class LoadSessionResponse(BaseModel):
    """
    Response from loading an existing session.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    modes: SessionModeState | None = Field(
        None,
        description="Initial mode state if supported by the Agent\n\nSee protocol docs: [Session Modes](https://agentclientprotocol.com/protocol/session-modes)",
    )
    config_options: (
        list[
            Annotated[
                SessionConfigOption1 | SessionConfigOption2, Field(discriminator="type")
            ]
        ]
        | None
    ) = Field(
        None,
        alias="configOptions",
        description="Initial session configuration options if supported by the Agent.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class ResumeSessionResponse(BaseModel):
    """
    Response from resuming an existing session.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    modes: SessionModeState | None = Field(
        None,
        description="Initial mode state if supported by the Agent\n\nSee protocol docs: [Session Modes](https://agentclientprotocol.com/protocol/session-modes)",
    )
    config_options: (
        list[
            Annotated[
                SessionConfigOption1 | SessionConfigOption2, Field(discriminator="type")
            ]
        ]
        | None
    ) = Field(
        None,
        alias="configOptions",
        description="Initial session configuration options if supported by the Agent.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class SetSessionConfigOptionResponse(BaseModel):
    """
    Response to `session/set_config_option` method.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    config_options: list[
        Annotated[
            SessionConfigOption1 | SessionConfigOption2, Field(discriminator="type")
        ]
    ] = Field(
        ...,
        alias="configOptions",
        description="The full set of configuration options and their current values.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class SessionUpdate1(ContentChunk):
    """
    A chunk of the user's message being streamed.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    session_update: Literal["user_message_chunk"] = Field(..., alias="sessionUpdate")


class SessionUpdate2(ContentChunk):
    """
    A chunk of the agent's response being streamed.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    session_update: Literal["agent_message_chunk"] = Field(..., alias="sessionUpdate")


class SessionUpdate3(ContentChunk):
    """
    A chunk of the agent's internal reasoning being streamed.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    session_update: Literal["agent_thought_chunk"] = Field(..., alias="sessionUpdate")


class SessionUpdate7(AvailableCommandsUpdate):
    """
    Available commands are ready or have changed
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    session_update: Literal["available_commands_update"] = Field(
        ..., alias="sessionUpdate"
    )


class ToolCall(BaseModel):
    """
    Represents a tool call that the language model has requested.

    Tool calls are actions that the agent executes on behalf of the language model,
    such as reading files, executing code, or fetching data from external sources.

    See protocol docs: [Tool Calls](https://agentclientprotocol.com/protocol/tool-calls)
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    tool_call_id: str = Field(
        ...,
        alias="toolCallId",
        description="Unique identifier for this tool call within the session.",
    )
    title: str = Field(
        ..., description="Human-readable title describing what the tool is doing."
    )
    name: str | None = Field(
        None,
        description="Programmatic name of the tool being invoked.\n\nThis field is optional. Omitting it or sending `null` both mean that no\ntool name is available.",
    )
    kind: ToolKind | None = Field(
        None,
        description="The category of tool being invoked.\nHelps clients choose appropriate icons and UI treatment.",
    )
    status: ToolCallStatus | None = Field(
        None, description="Current execution status of the tool call."
    )
    content: (
        list[
            Annotated[
                ToolCallContent1 | ToolCallContent2 | ToolCallContent3,
                Field(discriminator="type"),
            ]
        ]
        | None
    ) = Field(None, description="Content produced by the tool call.")
    locations: list[ToolCallLocation] | None = Field(
        None,
        description='File locations affected by this tool call.\nEnables "follow-along" features in clients.',
    )
    raw_input: Any | None = Field(
        None, alias="rawInput", description="Raw input parameters sent to the tool."
    )
    raw_output: Any | None = Field(
        None, alias="rawOutput", description="Raw output returned by the tool."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class ConfigOptionUpdate(BaseModel):
    """
    Session configuration options have been updated.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    config_options: list[
        Annotated[
            SessionConfigOption1 | SessionConfigOption2, Field(discriminator="type")
        ]
    ] = Field(
        ...,
        alias="configOptions",
        description="The full set of configuration options and their current values.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class ClientCapabilities(BaseModel):
    """
    Capabilities supported by the client.

    Advertised during initialization to inform the agent about
    available features and methods.

    See protocol docs: [Client Capabilities](https://agentclientprotocol.com/protocol/initialization#client-capabilities)
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    fs: FileSystemCapabilities | None = Field(
        {"readTextFile": False, "writeTextFile": False},
        description="File system capabilities supported by the client.\nDetermines which file operations the agent can request.",
        validate_default=True,
    )
    terminal: bool | None = Field(
        False, description="Whether the Client support all `terminal/*` methods."
    )
    session: ClientSessionCapabilities | None = Field(
        None,
        description="Session-related capabilities supported by the client.\n\nOptional. Omitted or `null` both mean the client does not advertise any\nsession-related extensions.",
    )
    auth: AuthCapabilities | None = Field(
        {"terminal": False},
        description="Authentication capabilities supported by the client.\nDetermines which authentication method types the agent may include\nin its `InitializeResponse`.",
        validate_default=True,
    )
    elicitation: ElicitationCapabilities | None = Field(
        None,
        description="Elicitation capabilities supported by the client.\nDetermines which elicitation modes the agent may use.\n\nOptional. Omitted or `null` both mean the client does not advertise\nelicitation support.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class ClientResponse1(BaseModel):
    """
    A successful JSON-RPC response.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    id: int | str | None = Field(
        ..., description="The id of the request this response answers."
    )
    result: (
        WriteTextFileResponse
        | ReadTextFileResponse
        | RequestPermissionResponse
        | CreateTerminalResponse
        | TerminalOutputResponse
        | ReleaseTerminalResponse
        | WaitForTerminalExitResponse
        | KillTerminalResponse
        | CreateElicitationResponse1
        | CreateElicitationResponse2
        | CreateElicitationResponse3
        | CreateElicitationResponse4
        | Any
    ) = Field(..., description="Method-specific response data.")


class ClientResponse(RootModel[ClientResponse1 | ClientResponse2]):
    root: ClientResponse1 | ClientResponse2 = Field(
        ..., description="A JSON-RPC response object."
    )


class ToolCallUpdate(BaseModel):
    """
    An update to an existing tool call.

    Used to report progress and results as tools execute. All fields except
    the tool call ID are optional - only changed fields need to be included.

    See protocol docs: [Updating](https://agentclientprotocol.com/protocol/tool-calls#updating)
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    tool_call_id: str = Field(
        ..., alias="toolCallId", description="The ID of the tool call being updated."
    )
    kind: ToolKind | None = Field(None, description="Update the tool kind.")
    status: ToolCallStatus | None = Field(
        None, description="Update the execution status."
    )
    title: str | None = Field(None, description="Update the human-readable title.")
    name: str | None = Field(
        None,
        description="Update the programmatic name of the tool being invoked.\n\nThis field is optional. Omitting it or sending `null` both mean that\nthe existing name is left unchanged.",
    )
    content: (
        list[
            Annotated[
                ToolCallContent1 | ToolCallContent2 | ToolCallContent3,
                Field(discriminator="type"),
            ]
        ]
        | None
    ) = Field(None, description="Replace the content collection.")
    locations: list[ToolCallLocation] | None = Field(
        None, description="Replace the locations collection."
    )
    raw_input: Any | None = Field(
        None, alias="rawInput", description="Update the raw input."
    )
    raw_output: Any | None = Field(
        None, alias="rawOutput", description="Update the raw output."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class CreateElicitationRequest11(ElicitationSessionScope):
    """
    Tied to a session, optionally to a specific tool call within that session.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    requested_schema: ElicitationSchema = Field(
        ...,
        alias="requestedSchema",
        description="A JSON Schema describing the form fields to present to the user.",
    )


class CreateElicitationRequest12(ElicitationRequestScope):
    """
    Tied to a specific JSON-RPC request outside of a session
    (e.g., during auth/configuration phases before any session is started).
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    requested_schema: ElicitationSchema = Field(
        ...,
        alias="requestedSchema",
        description="A JSON Schema describing the form fields to present to the user.",
    )


class CreateElicitationRequest14(
    CreateElicitationRequest11, CreateElicitationRequest13
):
    """
    Form-based elicitation where the client renders a form from the provided schema.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    message: str = Field(
        ..., description="A human-readable message describing what input is needed."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nOptional. Omitted and `null` are equivalent and mean no metadata.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )
    mode: Literal["form"]


class CreateElicitationRequest15(
    CreateElicitationRequest12, CreateElicitationRequest13
):
    """
    Form-based elicitation where the client renders a form from the provided schema.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    message: str = Field(
        ..., description="A human-readable message describing what input is needed."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nOptional. Omitted and `null` are equivalent and mean no metadata.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )
    mode: Literal["form"]


class NewSessionResponse(BaseModel):
    """
    Response from creating a new session.

    See protocol docs: [Creating a Session](https://agentclientprotocol.com/protocol/session-setup#creating-a-session)
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    session_id: str = Field(
        ...,
        alias="sessionId",
        description="Unique identifier for the created session.\n\nUsed in all subsequent requests for this conversation.",
    )
    modes: SessionModeState | None = Field(
        None,
        description="Initial mode state if supported by the Agent\n\nSee protocol docs: [Session Modes](https://agentclientprotocol.com/protocol/session-modes)",
    )
    config_options: (
        list[
            Annotated[
                SessionConfigOption1 | SessionConfigOption2, Field(discriminator="type")
            ]
        ]
        | None
    ) = Field(
        None,
        alias="configOptions",
        description="Initial session configuration options if supported by the Agent.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class SessionUpdate4(ToolCall):
    """
    Notification that a new tool call has been initiated.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    session_update: Literal["tool_call"] = Field(..., alias="sessionUpdate")


class SessionUpdate5(ToolCallUpdate):
    """
    Update on the status or results of a tool call.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    session_update: Literal["tool_call_update"] = Field(..., alias="sessionUpdate")


class SessionUpdate9(ConfigOptionUpdate):
    """
    Session configuration options have been updated.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    session_update: Literal["config_option_update"] = Field(..., alias="sessionUpdate")


class InitializeRequest(BaseModel):
    """
    Request parameters for the initialize method.

    Sent by the client to establish connection and negotiate capabilities.

    See protocol docs: [Initialization](https://agentclientprotocol.com/protocol/initialization)
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    protocol_version: conint(ge=0, le=65535) = Field(
        ...,
        alias="protocolVersion",
        description="The latest protocol version supported by the client.",
    )
    client_capabilities: ClientCapabilities | None = Field(
        {
            "fs": {"readTextFile": False, "writeTextFile": False},
            "terminal": False,
            "auth": {"terminal": False},
        },
        alias="clientCapabilities",
        description="Capabilities supported by the client.",
        validate_default=True,
    )
    client_info: Implementation | None = Field(
        None,
        alias="clientInfo",
        description="Information about the Client name and version sent to the Agent.\n\nNote: in future versions of the protocol, this will be required.",
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class AgentClientProtocol21(BaseModel):
    """
    A successful JSON-RPC response.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    id: int | str | None = Field(
        ..., description="The id of the request this response answers."
    )
    result: (
        InitializeResponse
        | AuthenticateResponse
        | LogoutResponse
        | NewSessionResponse
        | LoadSessionResponse
        | ListSessionsResponse
        | DeleteSessionResponse
        | ResumeSessionResponse
        | CloseSessionResponse
        | SetSessionModeResponse
        | SetSessionConfigOptionResponse
        | PromptResponse
        | Any
    ) = Field(..., description="Method-specific response data.")


class AgentClientProtocol24(AgentClientProtocol21, AgentClientProtocol23):
    """
    A message (request, response, or notification) with `"jsonrpc": "2.0"` specified as
    [required by JSON-RPC 2.0 Specification][1].

    [1]: https://www.jsonrpc.org/specification#compatibility
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    jsonrpc: Jsonrpc


class RequestPermissionRequest(BaseModel):
    """
    Request for user permission to execute a tool call.

    Sent when the agent needs authorization before performing a sensitive operation.

    See protocol docs: [Requesting Permission](https://agentclientprotocol.com/protocol/tool-calls#requesting-permission)
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    session_id: str = Field(
        ..., alias="sessionId", description="The session ID for this request."
    )
    tool_call: ToolCallUpdate = Field(
        ...,
        alias="toolCall",
        description="Details about the tool call requiring permission.",
    )
    options: list[PermissionOption] = Field(
        ..., description="Available permission options for the user to choose from."
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class AgentResponse1(BaseModel):
    """
    A successful JSON-RPC response.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    id: int | str | None = Field(
        ..., description="The id of the request this response answers."
    )
    result: (
        InitializeResponse
        | AuthenticateResponse
        | LogoutResponse
        | NewSessionResponse
        | LoadSessionResponse
        | ListSessionsResponse
        | DeleteSessionResponse
        | ResumeSessionResponse
        | CloseSessionResponse
        | SetSessionModeResponse
        | SetSessionConfigOptionResponse
        | PromptResponse
        | Any
    ) = Field(..., description="Method-specific response data.")


class AgentResponse(RootModel[AgentResponse1 | AgentResponse2]):
    root: AgentResponse1 | AgentResponse2 = Field(
        ..., description="A JSON-RPC response object."
    )


class SessionNotification(BaseModel):
    """
    Notification containing a session update from the agent.

    Used to stream real-time progress and results during prompt processing.

    See protocol docs: [Agent Reports Output](https://agentclientprotocol.com/protocol/prompt-turn#3-agent-reports-output)
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    session_id: str = Field(
        ...,
        alias="sessionId",
        description="The ID of the session this update pertains to.",
    )
    update: (
        SessionUpdate1
        | SessionUpdate2
        | SessionUpdate3
        | SessionUpdate4
        | SessionUpdate5
        | SessionUpdate6
        | SessionUpdate7
        | SessionUpdate8
        | SessionUpdate9
        | SessionUpdate10
        | SessionUpdate11
    ) = Field(
        ..., description="The actual update content.", discriminator="session_update"
    )
    field_meta: dict[str, Any] | None = Field(
        None,
        alias="_meta",
        description="The _meta property is reserved by ACP to allow clients and agents to attach additional\nmetadata to their interactions. Implementations MUST NOT make assumptions about values at\nthese keys.\n\nSee protocol docs: [Extensibility](https://agentclientprotocol.com/protocol/extensibility)",
    )


class ClientRequest(BaseModel):
    """
    A JSON-RPC request object.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    id: int | str | None = Field(
        ..., description="The request id used to correlate the matching response."
    )
    method: str = Field(..., description="The method name to invoke.")
    params: (
        InitializeRequest
        | AuthenticateRequest
        | LogoutRequest
        | NewSessionRequest
        | LoadSessionRequest
        | ListSessionsRequest
        | DeleteSessionRequest
        | ResumeSessionRequest
        | CloseSessionRequest
        | SetSessionModeRequest
        | PromptRequest
        | SetSessionConfigOptionRequest1
        | SetSessionConfigOptionRequest2
        | Any
        | None
    ) = Field(None, description="Method-specific request parameters.")


class AgentClientProtocol4(ClientRequest):
    """
    A message (request, response, or notification) with `"jsonrpc": "2.0"` specified as
    [required by JSON-RPC 2.0 Specification][1].

    [1]: https://www.jsonrpc.org/specification#compatibility
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    jsonrpc: Jsonrpc


class AgentRequest(BaseModel):
    """
    A JSON-RPC request object.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    id: int | str | None = Field(
        ..., description="The request id used to correlate the matching response."
    )
    method: str = Field(..., description="The method name to invoke.")
    params: (
        WriteTextFileRequest
        | ReadTextFileRequest
        | RequestPermissionRequest
        | CreateTerminalRequest
        | TerminalOutputRequest
        | ReleaseTerminalRequest
        | WaitForTerminalExitRequest
        | KillTerminalRequest
        | CreateElicitationRequest3
        | CreateElicitationRequest4
        | CreateElicitationRequest14
        | CreateElicitationRequest15
        | CreateElicitationRequest24
        | CreateElicitationRequest25
        | Any
        | None
    ) = Field(None, description="Method-specific request parameters.")


class AgentNotification(BaseModel):
    """
    A JSON-RPC notification object.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    method: str = Field(..., description="The notification method name.")
    params: SessionNotification | CompleteElicitationNotification | Any | None = Field(
        None, description="Method-specific notification parameters."
    )


class AgentClientProtocol1(AgentRequest):
    """
    A message (request, response, or notification) with `"jsonrpc": "2.0"` specified as
    [required by JSON-RPC 2.0 Specification][1].

    [1]: https://www.jsonrpc.org/specification#compatibility
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    jsonrpc: Jsonrpc


class AgentClientProtocol3(AgentNotification):
    """
    A message (request, response, or notification) with `"jsonrpc": "2.0"` specified as
    [required by JSON-RPC 2.0 Specification][1].

    [1]: https://www.jsonrpc.org/specification#compatibility
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
    )
    jsonrpc: Jsonrpc


class AgentClientProtocol(
    RootModel[
        AgentClientProtocol1
        | AgentClientProtocol3
        | AgentClientProtocol24
        | AgentClientProtocol25
        | AgentClientProtocol4
        | AgentClientProtocol6
        | AgentClientProtocol54
        | AgentClientProtocol55
        | AgentClientProtocol7
    ]
):
    root: (
        AgentClientProtocol1
        | AgentClientProtocol3
        | AgentClientProtocol24
        | AgentClientProtocol25
        | AgentClientProtocol4
        | AgentClientProtocol6
        | AgentClientProtocol54
        | AgentClientProtocol55
        | AgentClientProtocol7
    ) = Field(..., title="Agent Client Protocol")
