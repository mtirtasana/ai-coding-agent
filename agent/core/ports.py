from typing import Protocol, Sequence

from .types import ContextSelection, Message, ModelResponse, ToolCall, ToolResult, ToolSpec


class ModelProvider(Protocol):
    def generate(self, messages: Sequence[Message], tools: Sequence[ToolSpec]) -> ModelResponse: ...


class ContextBuilder(Protocol):
    def build(self, messages: Sequence[Message], tools: Sequence[ToolSpec],
              max_chars: int) -> ContextSelection: ...


class ToolExecutor(Protocol):
    def specifications(self) -> Sequence[ToolSpec]: ...
    def execute(self, call: ToolCall) -> ToolResult: ...


class EventSink(Protocol):
    def emit(self, kind: str, data: dict) -> None: ...
