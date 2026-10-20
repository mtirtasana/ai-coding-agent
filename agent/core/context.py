"""Deterministic projections of history; never truncate messages or mutate sources."""
import json
from collections.abc import Sequence

from .types import ContextSelection, ContextStats, Message, ToolSpec


class ContextLimitError(RuntimeError):
    pass


def message_chars(message: Message) -> int:
    return len(message.content) + sum(len(call.name) + len(call.arguments)
                                      for call in message.tool_calls)


def context_chars(messages: Sequence[Message], tools: Sequence[ToolSpec]) -> int:
    """Approximate character count, deliberately not a model-token estimate."""
    return (sum(message_chars(message) for message in messages)
            + sum(len(tool.description) + len(json.dumps(tool.parameters)) for tool in tools))


def split_rounds(messages: Sequence[Message]):
    """Validate a system/user prefix and complete assistant/tool rounds."""
    index = 0
    while index < len(messages) and messages[index].role == "system":
        if messages[index].tool_calls or messages[index].call_id is not None:
            raise ValueError("Systemnachricht enthält Werkzeugdaten.")
        index += 1
    if index == len(messages) or messages[index].role != "user":
        raise ValueError("Kontext benötigt eine ursprüngliche Nutzeraufgabe.")
    if messages[index].tool_calls or messages[index].call_id is not None:
        raise ValueError("Nutzeraufgabe enthält Werkzeugdaten.")
    index += 1
    prefix = tuple(messages[:index])
    rounds = []
    while index < len(messages):
        start = index
        assistant = messages[index]
        if assistant.role != "assistant" or assistant.call_id is not None:
            raise ValueError("Erwartete Assistant-Nachricht am Rundenanfang.")
        expected = {call.id for call in assistant.tool_calls}
        if len(expected) != len(assistant.tool_calls):
            raise ValueError("Doppelte Werkzeugaufruf-ID innerhalb einer Runde.")
        index += 1
        seen = set()
        while index < len(messages) and messages[index].role == "tool":
            result = messages[index]
            if (result.tool_calls or result.call_id not in expected
                    or result.call_id in seen):
                raise ValueError("Werkzeugergebnis ohne eindeutigen zugehörigen Aufruf.")
            seen.add(result.call_id)
            index += 1
        if seen != expected:
            raise ValueError("Unvollständige Werkzeugrunde im Kontext.")
        rounds.append(tuple(messages[start:index]))
    return prefix, rounds


class RuleBasedContextBuilder:
    def __init__(self, mode: str = "recent"):
        if mode not in {"recent", "full"}:
            raise ValueError("Kontextmodus muss recent oder full sein.")
        self.mode = mode

    def build(self, messages: Sequence[Message], tools: Sequence[ToolSpec],
              max_chars: int) -> ContextSelection:
        if max_chars < 1:
            raise ValueError("Kontextbudget muss positiv sein.")
        prefix, rounds = split_rounds(messages)
        full_chars = context_chars(messages, tools)
        if self.mode == "full":
            if full_chars > max_chars:
                raise ContextLimitError("Vollständiger Kontext überschreitet das Zeichenbudget.")
            return ContextSelection(tuple(messages), ContextStats(
                full_chars, full_chars, len(messages), len(messages), 0))

        selected_chars = context_chars(prefix, tools)
        if selected_chars > max_chars:
            raise ContextLimitError("Systemanweisungen, Aufgabe und Werkzeuge überschreiten das Zeichenbudget.")
        kept = []
        for round_messages in reversed(rounds):
            size = sum(message_chars(message) for message in round_messages)
            if selected_chars + size > max_chars:
                if not kept:
                    raise ContextLimitError("Die neueste vollständige Runde passt nicht ins Zeichenbudget.")
                break
            kept.append(round_messages)
            selected_chars += size
        selected = prefix + tuple(message for group in reversed(kept) for message in group)
        return ContextSelection(selected, ContextStats(
            full_chars, selected_chars, len(messages), len(selected), len(rounds) - len(kept)))
