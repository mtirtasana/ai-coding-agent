from typing import Protocol, Sequence

from .types import Message, ToolCall, ToolResult, ToolSpec


class ModelProvider(Protocol):
    def generate(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> Message: ...


class ToolExecutor(Protocol):
    def specifications(self) -> Sequence[ToolSpec]: ...
    def execute(self, call: ToolCall) -> ToolResult: ...


class EventSink(Protocol):
    def emit(self, kind: str, data: dict) -> None: ...
