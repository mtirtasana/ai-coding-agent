"""Deterministic projections of history; never truncate messages or mutate sources."""
import json
from pathlib import PurePosixPath, PureWindowsPath
from dataclasses import replace
from collections.abc import Sequence

from .types import ContextSelection, ContextStats, Message, ToolSpec


class ContextLimitError(RuntimeError):
    pass


def message_chars(message: Message) -> int:
    return (len(message.content) + sum(len(call.name) + len(call.arguments)
                                      for call in message.tool_calls)
            + (len(message.continuation.payload) if message.continuation is not None else 0))


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
    def __init__(self, mode: str = "recent", pinned_paths=()):
        if mode not in {"recent", "full", "compact"}:
            raise ValueError("Kontextmodus muss recent, compact oder full sein.")
        self.mode = mode
        if (not isinstance(pinned_paths, (list, tuple, set)) or len(pinned_paths) > 16 or
                any(not isinstance(path, str) or not 1 <= len(path) <= 1024 or
                    PurePosixPath(path.replace('\\', '/')).is_absolute() or
                    bool(PureWindowsPath(path).drive) or
                    '..' in PurePosixPath(path.replace('\\', '/')).parts for path in pinned_paths)):
            raise ValueError('Context pins require at most 16 relative file paths.')
        self.pinned_paths = {PurePosixPath(path.replace('\\', '/')).as_posix() for path in pinned_paths}

    def pinned(self, message):
        for call in message.tool_calls:
            if call.name not in {'file_snapshot', 'read_file', 'read_file_range', 'batch_inspect'}:
                continue
            try:
                args = json.loads(call.arguments)
                names = [item['path'] for item in args['requests']] if call.name == 'batch_inspect' else [args['path']]
                if any(PurePosixPath(name.replace('\\', '/')).as_posix() in self.pinned_paths for name in names):
                    return True
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
        return False

    def build(self, messages: Sequence[Message], tools: Sequence[ToolSpec],
              max_chars: int) -> ContextSelection:
        if max_chars < 1:
            raise ValueError("Kontextbudget muss positiv sein.")
        fixed_count, turns = split_turns(messages)
        pinned_indices = set()
        for user_index, rounds in turns:
            matched = [group for group in rounds if self.pinned(messages[group[0]])]
            if matched:
                if any(messages[group[0]].continuation is not None for group in rounds):
                    matched = rounds
                pinned_indices.add(user_index)
                for group in matched:
                    pinned_indices.update(group)
        full_chars = context_chars(messages, tools)
        if self.mode == 'compact':
            # Projection only: original history, calls and the newest two rounds
            # remain intact. Never summarize arbitrary source text as instructions.
            rounds = [group for _, groups in turns for group in groups]
            projected = list(messages)
            active_indices = {i for group in turns[-1][1] for i in group}
            preserve_active = any(messages[group[0]].continuation is not None for group in turns[-1][1])
            for group in rounds[:-2]:
                if group[0] in pinned_indices or preserve_active and group[0] in active_indices:
                    continue
                calls = {call.id: call for call in messages[group[0]].tool_calls}
                for index in group[1:]:
                    message = messages[index]
                    call = calls.get(message.call_id)
                    if call is None or call.name not in {'file_snapshot', 'read_file_range', 'batch_inspect'}:
                        continue
                    try:
                        envelope = json.loads(message.content)
                        data = json.loads(envelope['content'])
                        if call.name == 'batch_inspect' and isinstance(data, dict) and envelope.get('ok') is True:
                            changed = False
                            for item in data.get('results', []):
                                if isinstance(item, dict) and item.get('ok') is True and isinstance(item.get('content'), str) and len(item['content']) >= 4000:
                                    del item['content']
                                    item.update(content_omitted=True, note='Historical range; recover archived bytes or read current source before relying on omitted details.')
                                    changed = True
                            if changed:
                                projected[index] = replace(message, content=json.dumps(
                                    {'ok': True, 'content': json.dumps(data, ensure_ascii=False)}, ensure_ascii=False))
                            continue
                        if (envelope.get('ok') is not True or not isinstance(data, dict) or
                                not isinstance(data.get('content'), str) or len(data['content']) < 4000 or
                                not isinstance(data.get('path'), str) or not isinstance(data.get('sha256'), str)):
                            continue
                        reference = {key: data[key] for key in ('path', 'sha256', 'start_line', 'end_line',
                                     'total_lines', 'documentVersion', 'receipt') if key in data}
                        reference.update(content_omitted=True,
                                         note='Historical read reference, not current source. Read the relevant range again before relying on omitted details or editing. This hash may be stale.')
                        content = json.dumps({'ok': True, 'content': json.dumps(reference, ensure_ascii=False)},
                                             ensure_ascii=False)
                        if len(content) < len(message.content):
                            projected[index] = replace(message, content=content)
                    except (ValueError, TypeError, KeyError):
                        continue
            messages = projected
        if self.mode == "full":
            if full_chars > max_chars:
                raise ContextLimitError("Vollständiger Kontext überschreitet das Zeichenbudget.")
            return ContextSelection(tuple(messages), ContextStats(
                full_chars, full_chars, len(messages), len(messages), 0))

        # Pin runtime/project rules, accepted plan, original task and latest request.
        selected_indices = set(range(fixed_count)) | {turns[0][0], turns[-1][0]}
        selected_indices.update(pinned_indices)
        selected_chars = context_chars([messages[i] for i in sorted(selected_indices)], tools)
        if selected_chars > max_chars:
            raise ContextLimitError("Systemanweisungen, Aufgaben und Werkzeuge überschreiten das Zeichenbudget.")
        kept_rounds = 0
        active_kept = 0
        active_rounds = turns[-1][1]
        active_complete = True
        preserve_active = any(messages[group[0]].continuation is not None for group in active_rounds)
        for group in reversed(active_rounds):
            size = sum(message_chars(messages[i]) for i in group if i not in selected_indices)
            if selected_chars + size > max_chars:
                if preserve_active:
                    raise ContextLimitError("Responses continuation requires the complete active tool chain.")
                if active_kept == 0:
                    raise ContextLimitError("Die neueste vollständige Runde passt nicht ins Zeichenbudget.")
                active_complete = False
                break
            selected_indices.update(group)
            selected_chars += size
            kept_rounds += 1
            active_kept += 1
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
        kept_rounds = sum(all(index in selected_indices for index in group) for _, rounds in turns for group in rounds)
        total_rounds = sum(len(rounds) for _, rounds in turns)
        return ContextSelection(selected, ContextStats(
            full_chars, selected_chars, len(messages), len(selected), total_rounds - kept_rounds))

