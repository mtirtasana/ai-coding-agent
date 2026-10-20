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
    """Approximate characters, not model tokens or complete wire-format size."""
    return (sum(message_chars(message) for message in messages)
            + sum(len(tool.description) + len(json.dumps(tool.parameters)) for tool in tools))


def split_turns(messages: Sequence[Message]):
    """Return fixed-prefix length and turns as user index + complete round indices."""
    index = 0
    while index < len(messages) and messages[index].role == "system":
        if messages[index].tool_calls or messages[index].call_id is not None:
            raise ValueError("Systemnachricht enthält Werkzeugdaten.")
        index += 1
    fixed_count = index
    turns = []
    while index < len(messages):
        user_index = index
        user = messages[index]
        if user.role != "user" or user.tool_calls or user.call_id is not None:
            raise ValueError("Kontext benötigt eine gültige Nutzeraufgabe pro Turn.")
        index += 1
        rounds = []
        while index < len(messages) and messages[index].role != "user":
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
            rounds.append(tuple(range(start, index)))
        if index < len(messages) and (not rounds or messages[rounds[-1][0]].tool_calls):
            raise ValueError("Vor einem Folgeauftrag muss der vorherige Turn abgeschlossen sein.")
        turns.append((user_index, rounds))
    if not turns:
        raise ValueError("Kontext benötigt eine ursprüngliche Nutzeraufgabe.")
    return fixed_count, turns


class RuleBasedContextBuilder:
    def __init__(self, mode: str = "recent"):
        if mode not in {"recent", "full"}:
            raise ValueError("Kontextmodus muss recent oder full sein.")
        self.mode = mode

    def build(self, messages: Sequence[Message], tools: Sequence[ToolSpec],
              max_chars: int) -> ContextSelection:
        if max_chars < 1:
            raise ValueError("Kontextbudget muss positiv sein.")
        fixed_count, turns = split_turns(messages)
        full_chars = context_chars(messages, tools)
        if self.mode == "full":
            if full_chars > max_chars:
                raise ContextLimitError("Vollständiger Kontext überschreitet das Zeichenbudget.")
            return ContextSelection(tuple(messages), ContextStats(
                full_chars, full_chars, len(messages), len(messages), 0))

        # Pin runtime/project rules, accepted plan, original task and latest request.
        selected_indices = set(range(fixed_count)) | {turns[0][0], turns[-1][0]}
        selected_chars = context_chars([messages[i] for i in sorted(selected_indices)], tools)
        if selected_chars > max_chars:
            raise ContextLimitError("Systemanweisungen, Aufgaben und Werkzeuge überschreiten das Zeichenbudget.")
        kept_rounds = 0
        active_rounds = turns[-1][1]
        active_complete = True
        for group in reversed(active_rounds):
            size = sum(message_chars(messages[i]) for i in group)
            if selected_chars + size > max_chars:
                if kept_rounds == 0:
                    raise ContextLimitError("Die neueste vollständige Runde passt nicht ins Zeichenbudget.")
                active_complete = False
                break
            selected_indices.update(group)
            selected_chars += size
            kept_rounds += 1
        # Older completed turns are optional but atomic, including their user request.
        if active_complete:
            for user_index, rounds in reversed(turns[:-1]):
                indices = {user_index} | {i for group in rounds for i in group}
                size = sum(message_chars(messages[i]) for i in indices - selected_indices)
                if selected_chars + size > max_chars:
                    break
                selected_indices.update(indices)
                selected_chars += size
                kept_rounds += len(rounds)
        selected = tuple(messages[i] for i in sorted(selected_indices))
        total_rounds = sum(len(rounds) for _, rounds in turns)
        return ContextSelection(selected, ContextStats(
            full_chars, selected_chars, len(messages), len(selected), total_rounds - kept_rounds))
