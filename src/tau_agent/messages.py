"""Pi-compatible provider-neutral content and transcript message models."""

from __future__ import annotations

from collections.abc import Iterable
from time import time
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tau_agent.types import JSONValue


def _to_camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(part.title() for part in parts[1:])


def current_timestamp_ms() -> int:
    """Return the current Unix timestamp in milliseconds."""
    return int(time() * 1000)


class WireModel(BaseModel):
    """Strict model with Python field names and Pi-compatible JSON aliases."""

    model_config = ConfigDict(
        # Don't accept unknown fields.
        extra="forbid",
        # Validate with python snake_case names.
        validate_by_name=True,
        # Validate with JSON camelCase alias.
        validate_by_alias=True,
        # Serialize to JSON camelCase.
        serialize_by_alias=True,
        # camelCase callable to transform name to alias.
        alias_generator=_to_camel,
    )


class UsageCost(WireModel):
    """Billed response cost in USD."""

    # Costs are estimated by taking token count + model pricing (if pricing is
    # available).

    # Input token cost.
    input: float = 0.0
    # Output token cost.
    output: float = 0.0
    # Cost for reading from cache.
    cache_read: float = 0.0
    # Cost for writing to cache.
    cache_write: float = 0.0
    # input + output + cache_read + cache_write
    total: float = 0.0


#  Request:
#     UserMessage("What is 2 + 2?")
#
#   Response:
#     AssistantMessage("4", usage=Usage(...))
#
#   Request:
#     UserMessage("What is 2 + 2?")
#     AssistantMessage("4", usage=...)
#     UserMessage("Multiply that by 3.")
#
#   Response:
#     AssistantMessage("12", usage=Usage(...))
class Usage(WireModel):
    """Provider-reported token usage for one assistant response."""

    # Token counts are provider-reported, not internally tracked. Not all may
    # be provided. Provider adapters will take what's provided by the provider
    # an attempt to normalize to this common token count format.

    # Input tokens count.
    input: int = 0
    # Output tokens count.
    output: int = 0
    # Tokens read from cache.
    cache_read: int = 0
    # Tokens written to cache.
    cache_write: int = 0
    # Tokens written to cache in the last 1 hour.
    cache_write_1h: int | None = None
    # Internal reasoning token count.
    reasoning: int | None = None
    total_tokens: int = 0
    cost: UsageCost = UsageCost()


def sum_usage(usages: Iterable[Usage]) -> Usage:
    """Return the field-wise total for one or more provider requests."""
    # Why the fuck wasn't this a list instead.
    items = tuple(usages)

    def optional_total(field: Literal["cache_write_1h", "reasoning"]) -> int | None:
        values = [getattr(item, field) for item in items]
        return (
            sum(value for value in values if value is not None)
            if any(value is not None for value in values)
            else None
        )

    return Usage(
        input=sum(item.input for item in items),
        output=sum(item.output for item in items),
        cache_read=sum(item.cache_read for item in items),
        cache_write=sum(item.cache_write for item in items),
        cache_write_1h=optional_total("cache_write_1h"),
        reasoning=optional_total("reasoning"),
        total_tokens=sum(item.total_tokens for item in items),
        cost=UsageCost(
            input=sum(item.cost.input for item in items),
            output=sum(item.cost.output for item in items),
            cache_read=sum(item.cost.cache_read for item in items),
            cache_write=sum(item.cost.cache_write for item in items),
            total=sum(item.cost.total for item in items),
        ),
    )


# Each AssistantMessage has a Usage and ResponseTiming associated with it.
class ResponseTiming(WireModel):
    """Monotonic request durations for one assistant response."""

    time_to_first_output_ms: int | None = Field(default=None, ge=0)
    total_duration_ms: int = Field(ge=0)


# AssistantMessage.content is an ordered list of content blocks:
#
#    type AssistantContent = TextContent | ThinkingContent | ToolCall
#
#    class AssistantMessage(WireModel):
#        content: list[AssistantContent] = ...
#
# For example:
#
#    AssistantMessage(content=[
#        ThinkingContent(thinking="Let me work this out..."),
#        TextContent(text="The answer is 42."),
#    ])
#
#  One assistant message can contain multiple blocks:
#  - TextContent: Answer text.
#  - ThinkingContent: Provider-exposed thinking, possibly redacted.
#  - ToolCall: A request to execute a tool.
#
#  ImageContent is not allowed in AssistantMessage.content in this model. It
#  belongs in user messages and tool results—for example, an uploaded image or an
#  image returned by a file-reading tool.


class TextContent(WireModel):
    type: Literal["text"] = "text"
    text: str
    # text_signature: "This text really came from this model and hasn’t been
    # edited."
    text_signature: str | None = None


class ThinkingContent(WireModel):
    type: Literal["thinking"] = "thinking"
    thinking: str
    # thinking_signature: "This thinking really came from this model and hasn’t
    # been edited."
    thinking_signature: str | None = None
    redacted: bool = False


class ImageContent(WireModel):
    type: Literal["image"] = "image"
    # Base64-encoded image bytes.
    data: str
    # image/jpeg image/png etc.
    mime_type: str


class ToolCall(WireModel):
    """A tool call content block requested by the assistant."""

    type: Literal["toolCall"] = "toolCall"
    id: str
    name: str
    arguments: dict[str, JSONValue] = Field(default_factory=dict)
    thought_signature: str | None = None


type UserContent = str | list[TextContent | ImageContent]
type AssistantContent = TextContent | ThinkingContent | ToolCall
type ToolResultContent = TextContent | ImageContent


class UserMessage(WireModel):
    role: Literal["user"] = "user"
    content: UserContent
    timestamp: int = Field(default_factory=current_timestamp_ms)

    @property
    def text(self) -> str:
        return content_text(self.content)


class AssistantDiagnosticError(WireModel):
    name: str | None = None
    message: str
    stack: str | None = None
    code: str | int | None = None


class AssistantMessageDiagnostic(WireModel):
    type: str
    timestamp: int = Field(default_factory=current_timestamp_ms)
    error: AssistantDiagnosticError | None = None
    details: dict[str, JSONValue] | None = None


StopReason = Literal["stop", "length", "toolUse", "error", "aborted"]


class AssistantMessage(WireModel):
    """A Pi-compatible assistant message with ordered content blocks."""

    role: Literal["assistant"] = "assistant"
    content: list[AssistantContent] = Field(default_factory=list)
    api: str = "unknown"
    provider: str = "unknown"
    model: str = "unknown"
    response_model: str | None = None
    response_provider: str | None = None
    response_id: str | None = None
    diagnostics: list[AssistantMessageDiagnostic] | None = None
    usage: Usage = Usage()
    timing: ResponseTiming | None = None
    stop_reason: StopReason = "stop"
    error_message: str | None = None
    timestamp: int = Field(default_factory=current_timestamp_ms)

    @model_validator(mode="before")
    @classmethod
    def _normalize_convenient_content(cls, value: object) -> object:
        """Accept a string only as a Python construction convenience.

        The stored model and serialized protocol are always block based. This
        keeps provider and test construction concise without creating a second
        message representation.
        """
        if not isinstance(value, dict):
            return value
        data = dict(value)
        content = data.get("content")
        if isinstance(content, str):
            data["content"] = [TextContent(text=content)] if content else []
        usage = data.get("usage")
        if usage is None:
            data["usage"] = Usage()
        return data

    @property
    def text(self) -> str:
        return "".join(block.text for block in self.content if isinstance(block, TextContent))

    @property
    def thinking_text(self) -> str:
        return "".join(
            block.thinking for block in self.content if isinstance(block, ThinkingContent)
        )

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        return tuple(block for block in self.content if isinstance(block, ToolCall))


class ToolResultMessage(WireModel):
    role: Literal["toolResult"] = "toolResult"
    tool_call_id: str
    tool_name: str
    content: list[ToolResultContent] = Field(default_factory=list)
    details: JSONValue = None
    added_tool_names: list[str] | None = None
    is_error: bool = False
    timestamp: int = Field(default_factory=current_timestamp_ms)

    @model_validator(mode="before")
    @classmethod
    def _normalize_convenient_content(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        content = data.get("content")
        if isinstance(content, str):
            data["content"] = [TextContent(text=content)] if content else []
        return data

    @property
    def text(self) -> str:
        return content_text(self.content)


class BashExecutionMessage(WireModel):
    role: Literal["bashExecution"] = "bashExecution"
    command: str
    output: str
    exit_code: int | None = None
    cancelled: bool = False
    truncated: bool = False
    full_output_path: str | None = None
    timestamp: int = Field(default_factory=current_timestamp_ms)
    exclude_from_context: bool = False


class CustomMessage(WireModel):
    role: Literal["custom"] = "custom"
    custom_type: str
    content: UserContent
    display: bool = True
    details: JSONValue = None
    timestamp: int = Field(default_factory=current_timestamp_ms)

    @property
    def text(self) -> str:
        return content_text(self.content)


class BranchSummaryMessage(WireModel):
    role: Literal["branchSummary"] = "branchSummary"
    summary: str
    from_id: str
    timestamp: int = Field(default_factory=current_timestamp_ms)


class CompactionSummaryMessage(WireModel):
    role: Literal["compactionSummary"] = "compactionSummary"
    summary: str
    tokens_before: int
    timestamp: int = Field(default_factory=current_timestamp_ms)


type AgentMessage = Annotated[
    UserMessage
    | AssistantMessage
    | ToolResultMessage
    | BashExecutionMessage
    | CustomMessage
    | BranchSummaryMessage
    | CompactionSummaryMessage,
    Field(discriminator="role"),
]


def assistant_content(
    text: str,
    tool_calls: list[ToolCall] | tuple[ToolCall, ...] = (),
) -> list[AssistantContent]:
    """Build canonical ordered assistant blocks from parser accumulators."""
    blocks: list[AssistantContent] = [TextContent(text=text)] if text else []
    blocks.extend(tool_calls)
    return blocks


def content_text(content: str | list[Any]) -> str:
    """Return visible text from string or text/image content."""
    if isinstance(content, str):
        return content
    return "".join(block.text for block in content if isinstance(block, TextContent))


def message_to_user(message: AgentMessage) -> UserMessage:
    """Convert custom/session-only messages to provider-compatible user context."""
    return UserMessage(content=message_text(message), timestamp=message.timestamp)


def message_text(message: AgentMessage) -> str:
    """Return the user-visible text represented by an agent message."""
    if isinstance(message, (UserMessage, AssistantMessage, ToolResultMessage, CustomMessage)):
        return message.text
    if isinstance(message, (BranchSummaryMessage, CompactionSummaryMessage)):
        return message.summary
    if isinstance(message, BashExecutionMessage):
        return message.output
    return ""
