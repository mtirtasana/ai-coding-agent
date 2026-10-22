import json
import tempfile
import unittest
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace as NS
from uuid import uuid4

from agent.adapters.models import OpenAIProvider, OpenAICompatibleProvider
from agent.adapters.recovery import RecoveryStore
from agent.core.context import ContextLimitError, RuleBasedContextBuilder, context_chars
from agent.core.engine import Agent, OutputLimitError
from agent.core.types import Continuation, ConversationState, Message, ToolResult, ToolSpec
from agent.files import Workspace
from agent.server import restore_state


class OutputItem(NS):
    def model_dump(self, **kwargs):
        return deepcopy(vars(self))


def output(round_id, *, calls=1, text=True):
    items = [{"type": "reasoning", "id": f"rs{round_id}", "summary": [],
              "encrypted_content": f"opaque-{round_id}"}]
    if text:
        items.append({"type": "message", "id": f"msg{round_id}", "role": "assistant",
                      "status": "completed", "content": [
                          {"type": "output_text", "text": "Working", "annotations": []}]})
    for index in range(calls):
        items.append({"type": "function_call", "id": f"fc{round_id}-{index}",
                      "call_id": f"call{round_id}-{index}", "name": "read_file_range",
                      "arguments": "{}", "status": "completed"})
    return items


class ResponsesTests(unittest.TestCase):
    def provider(self, outputs, *, status="completed"):
        self.sent = []
        queue = iter(outputs)

        def create(**kwargs):
            self.sent.append(deepcopy(kwargs))
            return NS(output=[OutputItem(**item) for item in next(queue)],
                      output_text="Working", status=status,
                      usage=NS(input_tokens=10, output_tokens=0,
                               input_tokens_details=NS(cached_tokens=2)))
        return OpenAIProvider("test", client=NS(responses=NS(create=create)))

    def history(self):
        provider = self.provider([output(1), output(2), output(3)])
        messages = [Message("user", "Task")]
        for _ in range(3):
            message = provider.generate(messages, []).message
            messages.extend([message, Message("tool", "result", call_id=message.tool_calls[0].id)])
        return messages

    def test_two_tool_rounds_preserve_every_item_once_and_in_order(self):
        first, second, final = output(1, calls=2), output(2), output(3, calls=0)
        provider = self.provider([first, second, final])
        history = [Message("user", "Task")]
        expected = [{"role": "user", "content": "Task"}]
        for items in (first, second, final):
            result = provider.generate(history, [])
            self.assertEqual(self.sent[-1]["input"], expected)
            self.assertFalse(self.sent[-1]["store"])
            self.assertNotIn("previous_response_id", self.sent[-1])
            self.assertEqual(self.sent[-1]["include"], ["reasoning.encrypted_content"])
            self.assertEqual(json.loads(result.message.continuation.payload), items)
            self.assertEqual(result.usage.output_tokens, 0)
            self.assertEqual(result.usage.cached_input_tokens, 2)
            history.append(result.message)
            expected.extend(items)
            for call in result.message.tool_calls:
                history.append(Message("tool", "result", call_id=call.id))
                expected.append({"type": "function_call_output", "call_id": call.id, "output": "result"})

    def test_engine_preserves_two_rounds_and_commits_completed_turn(self):
        provider = self.provider([output(1), output(2), output(3, calls=0)])
        executed = []
        tools = NS(specifications=lambda: [ToolSpec("read_file_range", "Read", {})],
                   execute=lambda call: executed.append(call.id) or ToolResult(True, "result"))
        state = ConversationState()
        result = Agent(provider, tools).run("Task", state)
        self.assertEqual(result.steps, 3)
        self.assertEqual(executed, ["call1-0", "call2-0"])
        self.assertEqual([item["id"] for item in self.sent[2]["input"]
                          if item.get("type") == "reasoning"], ["rs1", "rs2"])
        self.assertEqual(len(state.messages), 6)
        self.assertIsNotNone(state.messages[-1].continuation)

    def test_sdk_output_serialization_roundtrip(self):
        try:
            from openai.types.responses import Response
        except ImportError:
            self.skipTest("Optional OpenAI SDK is not installed.")
        response = Response.model_validate({
            "id": "resp1", "created_at": 0, "model": "test", "object": "response",
            "output": output(1), "parallel_tool_calls": True, "tool_choice": "auto",
            "tools": [], "status": "completed",
        })
        sent = []
        def create(**kwargs):
            sent.append(kwargs)
            return response
        provider = OpenAIProvider("test", client=NS(responses=NS(create=create)))
        first = provider.generate([Message("user", "Task")], [])
        provider.generate([first.message, Message("tool", "result", call_id="call1-0")], [])
        self.assertEqual(sent[1]["input"][:-1], [
            item.model_dump(mode="json", exclude_none=True) for item in response.output])

    def test_new_conversation_does_not_replay_previous_output(self):
        provider = self.provider([output(1), output(2, calls=0)])
        provider.generate([Message("user", "One")], [])
        provider.generate([Message("user", "Two")], [])
        self.assertEqual(self.sent[-1]["input"], [{"role": "user", "content": "Two"}])

    def test_context_preserves_active_chain_and_counts_payload(self):
        history = self.history()
        full = context_chars(history, [])
        for mode in ("recent", "compact", "full"):
            with self.subTest(mode=mode):
                builder = RuleBasedContextBuilder(mode)
                self.assertEqual(builder.build(history, [], full).messages, tuple(history))
                with self.assertRaises(ContextLimitError):
                    builder.build(history, [], full - 1)
        without = [replace(m, continuation=None) for m in history]
        self.assertEqual(full - context_chars(without, []),
                         sum(len(m.continuation.payload) for m in history if m.continuation))

    def test_compact_does_not_rewrite_active_responses_results(self):
        history = self.history()
        envelope = json.dumps({"ok": True, "content": json.dumps(
            {"path": "x", "sha256": "a" * 64, "content": "x" * 5000})})
        history[2] = replace(history[2], content=envelope)
        selected = RuleBasedContextBuilder("compact").build(history, [], 100000)
        self.assertEqual(selected.messages, tuple(history))

    def test_completed_old_turn_can_be_dropped_atomically(self):
        history = self.history()
        provider = self.provider([output(4, calls=0)])
        history.append(provider.generate(history, []).message)
        history.append(Message("user", "Next"))
        budget = context_chars([history[0], history[-1]], [])
        selected = RuleBasedContextBuilder().build(history, [], budget)
        self.assertEqual(selected.messages, (history[0], history[-1]))

    def completed_state(self):
        history = self.history()
        provider = self.provider([output(4, calls=0)])
        history.append(provider.generate(history, []).message)
        return ConversationState(history, "plan", "plan")

    def test_checkpoint_and_recovery_roundtrip(self):
        state = self.completed_state()
        self.assertEqual(restore_state(json.loads(json.dumps(asdict(state)))), state)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            project.mkdir()
            workspace = Workspace(project)
            store = RecoveryStore(root / "sessions")
            identifier = str(uuid4())
            store.save(identifier, workspace, state,
                       {"provider": "openai", "profile": "", "model": "test"})
            restored, _, _ = store.resume(identifier, workspace)
            self.assertEqual(restored.messages, state.messages)
            self.assertIsNone(restored.accepted_plan)
            provider = self.provider([output(5, calls=0)])
            provider.generate(restored.messages + [Message("user", "Continue")], [])
            self.assertIn(output(1)[0], self.sent[0]["input"])

    def test_legacy_checkpoint_without_continuation(self):
        state = ConversationState([Message("user", "Task"), Message("assistant", "Done")])
        raw = json.loads(json.dumps(asdict(state)))
        for message in raw["messages"]:
            del message["continuation"]
        self.assertEqual(restore_state(raw), state)

    def test_invalid_checkpoint_continuation_rejected(self):
        raw = json.loads(json.dumps(asdict(self.completed_state())))
        for payload in ("{}", "not json", '[{"type":"message","role":"system","content":[]}]',
                        '[{"type":"function_call","call_id":"wrong","name":"x","arguments":"{}"}]'):
            with self.subTest(payload=payload):
                bad = deepcopy(raw)
                bad["messages"][1]["continuation"]["payload"] = payload
                with self.assertRaises(ValueError):
                    restore_state(bad)
        for change in ({"format": "other", "payload": "[]"}, {"payload": "[]"}):
            bad = deepcopy(raw)
            bad["messages"][1]["continuation"] = change
            with self.assertRaises(ValueError):
                restore_state(bad)
        bad = deepcopy(raw)
        bad["messages"][0]["continuation"] = raw["messages"][1]["continuation"]
        with self.assertRaises(ValueError):
            restore_state(bad)

    def test_incomplete_does_not_execute_or_commit(self):
        provider = self.provider([output(1)], status="incomplete")
        executed = []
        tools = NS(specifications=lambda: [ToolSpec("read_file_range", "Read", {})],
                   execute=lambda call: executed.append(call) or ToolResult(True, "result"))
        state = ConversationState()
        with self.assertRaises(OutputLimitError):
            Agent(provider, tools).run("Task", state)
        self.assertEqual(executed, [])
        self.assertEqual(state.messages, [])

    def test_compatible_provider_does_not_send_continuation(self):
        sent = []
        def create(**kwargs):
            sent.append(kwargs)
            return NS(choices=[NS(message=NS(content="Done", tool_calls=None))])
        provider = OpenAICompatibleProvider("test", "http://localhost/v1", "key",
                                            client=NS(chat=NS(completions=NS(create=create))))
        provider.generate([Message("assistant", "Text",
                                   continuation=Continuation("openai.responses.v1", "[]"))], [])
        self.assertEqual(sent[0]["messages"], [{"role": "assistant", "content": "Text"}])
