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
class ToolResult:
    ok: bool
    content: str


@dataclass
class RunResult:
    text: str
    steps: int
    messages: list[Message] = field(default_factory=list)
