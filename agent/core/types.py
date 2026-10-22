from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    read_only: bool = False


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class Continuation:
    """Opaque, immutable provider output; never displayed as assistant text."""
    format: str
    payload: str


@dataclass(frozen=True)
class Message:
    role: str
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    call_id: str | None = None
    continuation: Continuation | None = None


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
    truncated: bool = False


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
    estimated_input_tokens: int | None = None
    token_estimate_method: str | None = None


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    content: str


@dataclass(frozen=True)
class CheckEvidence:
    profile: str
    status: str  # passed, failed, stale, not_run, unknown
    fingerprint: str | None = None
    configuration: str | None = None


@dataclass
class RunResult:
    text: str
    steps: int
    messages: list[Message] = field(default_factory=list)
    requests: list[RequestMetrics] = field(default_factory=list)
    verification: list[CheckEvidence] = field(default_factory=list)


@dataclass
class ConversationState:
    """Only completed turns are committed; runtime instructions are supplied separately."""
    messages: list[Message] = field(default_factory=list)
    latest_plan: str | None = None
    accepted_plan: str | None = None
