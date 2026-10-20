import json
import unittest
from io import StringIO

from agent.adapters.events import JsonlEventSink
from agent.adapters.tools import ToolRegistry, spec
from agent.core.context import ContextLimitError, RuleBasedContextBuilder, context_chars
from agent.core.engine import Agent, INSTRUCTIONS
from agent.core.policy import MODE_INSTRUCTIONS
from agent.core.types import Message, ModelResponse, ModelUsage, ToolCall


def round_messages(number, size=20, calls=1):
    ids = [f"{number}-{i}" for i in range(calls)]
    return [Message("assistant", f"Round {number}", tuple(
        ToolCall(i, "echo", '{"text":"x"}') for i in ids)),
        *[Message("tool", "x" * size, call_id=i) for i in ids]]


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.prefix = [Message("system", "Rules"), Message("user", "Task")]
        self.builder = RuleBasedContextBuilder()

    def test_exact_budget_and_no_mutation(self):
        history = self.prefix + round_messages(1) + round_messages(2, calls=2)
        original = list(history)
        expected = self.prefix + round_messages(2, calls=2)
        budget = context_chars(expected, [])
        selection = self.builder.build(history, [], budget)
        self.assertEqual(selection.messages, tuple(expected))
        self.assertEqual(selection.stats.selected_chars, budget)
        self.assertEqual(selection.stats.dropped_rounds, 1)
        self.assertEqual(selection.stats.full_messages, len(history))
        self.assertEqual(history, original)
        self.assertEqual([m.call_id for m in selection.messages if m.role == "tool"], ["2-0", "2-1"])

    def test_returns_full_history_when_it_fits(self):
        history = self.prefix + round_messages(1) + round_messages(2)
        selection = self.builder.build(history, [], context_chars(history, []))
        self.assertEqual(selection.messages, tuple(history))
        self.assertEqual(selection.stats.dropped_rounds, 0)

    def test_never_skips_a_middle_round_to_keep_older_one(self):
        latest = round_messages(3, size=1)
        history = self.prefix + round_messages(1, size=1) + round_messages(2, size=500) + latest
        selection = self.builder.build(history, [], context_chars(self.prefix + latest, []) + 100)
        self.assertEqual(selection.messages, tuple(self.prefix + latest))
        self.assertEqual(selection.stats.dropped_rounds, 2)

    def test_latest_round_cannot_be_removed_or_truncated(self):
        history = self.prefix + round_messages(1, size=1000)
        with self.assertRaisesRegex(ContextLimitError, "neueste"):
            self.builder.build(history, [], 100)

    def test_tool_schemas_and_all_system_messages_count(self):
        tools = [spec("example", "d" * 100, {"text": {"type": "string"}})]
        history = [Message("system", "first"), Message("system", "second"), Message("user", "task")]
        budget = context_chars(history, tools)
        self.assertEqual(self.builder.build(history, tools, budget).messages, tuple(history))
        with self.assertRaises(ContextLimitError):
            self.builder.build(history, tools, budget - 1)

    def test_full_mode_preserves_legacy_stop(self):
        history = self.prefix + round_messages(1) + round_messages(2)
        budget = context_chars(self.prefix + round_messages(2), [])
        with self.assertRaises(ContextLimitError):
            RuleBasedContextBuilder("full").build(history, [], budget)
        selection = RuleBasedContextBuilder("full").build(history, [], 1000)
        self.assertEqual(selection.messages, tuple(history))

    def test_invalid_or_incomplete_protocol_is_rejected_before_selection(self):
        invalid = [
            self.prefix + [Message("tool", "orphan", call_id="a")],
            self.prefix + round_messages(1, calls=2)[:-1],
            self.prefix + round_messages(1) + [Message("tool", "duplicate", call_id="1-0")],
            self.prefix + [Message("assistant", tool_calls=(ToolCall("a", "echo", '{}'),
                                                            ToolCall("a", "echo", '{}')))],
            self.prefix + [Message("assistant", tool_calls=(ToolCall("a", "echo", '{}'),)),
                           Message("tool", "wrong id", call_id="b")],
        ]
        for mode in ["recent", "full"]:
            for history in invalid:
                with self.subTest(mode=mode, history=history), self.assertRaises(ValueError):
                    RuleBasedContextBuilder(mode).build(history, [], 10000)

    def test_invalid_config(self):
        with self.assertRaises(ValueError):
            RuleBasedContextBuilder("typo")
        with self.assertRaises(ValueError):
            self.builder.build(self.prefix, [], 0)


class LoopTests(unittest.TestCase):
    def tools(self):
        registry = ToolRegistry()
        registry.register(spec("echo", "Echo", {"text": {"type": "string"}}), lambda text: "x" * 200)
        return registry

    def model(self):
        class LongModel:
            def __init__(self):
                self.step = 0
                self.contexts = []

            def generate(self, messages, tools):
                self.step += 1
                self.contexts.append(tuple(messages))
                if self.step <= 8:
                    message = Message("assistant", tool_calls=(ToolCall(
                        str(self.step), "echo", json.dumps({"text": "x"})),))
                else:
                    message = Message("assistant", "Done")
                return ModelResponse(message, ModelUsage(input_tokens=self.step, output_tokens=1))
        return LongModel()

    def test_long_run_sends_less_context_and_keeps_full_history(self):
        registry = self.tools()
        # Fits prefix + one round, but not two.
        budget = context_chars([Message("system", INSTRUCTIONS), Message("system", MODE_INSTRUCTIONS["implement"]),
                                Message("user", "Task")],
                               registry.specifications()) + 350
        recent_model, full_model = self.model(), self.model()
        stream = StringIO()
        recent = Agent(recent_model, registry, JsonlEventSink(stream), max_steps=10,
                       max_context_chars=budget).run("Task")
        full = Agent(full_model, registry, max_steps=10, max_context_chars=100000,
                     context_builder=RuleBasedContextBuilder("full")).run("Task")
        self.assertEqual(recent.text, full.text)
        self.assertEqual(recent.messages, full.messages)
        self.assertEqual(len(recent.messages), 20)
        recent_chars = sum(r.context.selected_chars for r in recent.requests)
        full_chars = sum(r.context.selected_chars for r in full.requests)
        self.assertLess(recent_chars, full_chars)
        self.assertTrue(all(r.context.selected_chars <= budget for r in recent.requests))
        self.assertEqual(recent.requests[-1].usage.input_tokens, 9)
        events = [json.loads(line) for line in stream.getvalue().splitlines()]
        self.assertEqual(len([e for e in events if e["event"] == "context_selected"]), 9)
        self.assertGreater(recent.requests[-1].context.dropped_rounds, 0)
        for messages in recent_model.contexts:
            self.assertEqual(messages[:3], (Message("system", INSTRUCTIONS),
                                           Message("system", MODE_INSTRUCTIONS["implement"]),
                                           Message("user", "Task")))
            for i, message in enumerate(messages):
                if message.role == "tool":
                    self.assertEqual(messages[i - 1].tool_calls[0].id, message.call_id)
        print(f"\nSimulated context comparison: recent={recent_chars}, full={full_chars} chars")

    def test_limit_stops_before_network_and_emits_reason(self):
        model = self.model()
        stream = StringIO()
        with self.assertRaises(ContextLimitError):
            Agent(model, self.tools(), JsonlEventSink(stream), max_context_chars=1).run("Task")
        self.assertEqual(model.step, 0)
        self.assertEqual(json.loads(stream.getvalue().splitlines()[-1])["reason"], "context_limit")
