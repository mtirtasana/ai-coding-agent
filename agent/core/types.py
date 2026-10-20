from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class Message:
    role: str
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    call_id: str | None = None


@dataclass(frozen=True)
class ModelUsage:
    """Provider-reported counts; unknown fields stay None, never estimated."""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None


@dataclass(frozen=True)
class ModelResponse:
    message: Message
    usage: ModelUsage | None = None


@dataclass(frozen=True)
class ContextStats:
    full_chars: int
    selected_chars: int
    full_messages: int
    selected_messages: int
    dropped_rounds: int


@dataclass(frozen=True)
class ContextSelection:
    messages: tuple[Message, ...]
    stats: ContextStats


@dataclass(frozen=True)
class RequestMetrics:
    step: int
    context: ContextStats
    usage: ModelUsage | None


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    content: str


@dataclass
class RunResult:
    text: str
    steps: int
    messages: list[Message] = field(default_factory=list)
    requests: list[RequestMetrics] = field(default_factory=list)
