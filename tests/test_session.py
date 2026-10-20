import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path

from agent.adapters.events import JsonlEventSink
from agent.adapters.instructions import load_project_instructions
from agent.adapters.tools import workspace_tools
from agent.core.context import ContextLimitError, RuleBasedContextBuilder, context_chars
from agent.core.engine import Agent
from agent.core.types import ConversationState, Message, ModelResponse, ToolCall
from agent.files import Workspace


class RecordingModel:
    def __init__(self, reply="Done"):
        self.calls = []
        self.reply = reply

    def generate(self, messages, tools):
        self.calls.append((tuple(messages), tuple(tools)))
        return ModelResponse(Message("assistant", self.reply))


class SessionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = Workspace(self.root, allow_write=True)

    def test_plan_cannot_write_even_with_writable_executor(self):
        class WritingModel:
            def generate(self, messages, tools):
                if messages[-1].role == "user":
                    if any(tool.name == "write_file" for tool in tools):
                        raise AssertionError("Write tool exposed in plan mode")
                    return ModelResponse(Message("assistant", tool_calls=(ToolCall(
                        "w", "write_file", '{"path":"bad.txt","content":"bad"}'),)))
                self.result = json.loads(messages[-1].content)
                return ModelResponse(Message("assistant", "Plan only"))
        model = WritingModel()
        Agent(model, workspace_tools(self.workspace), mode="plan").run("Write")
        self.assertFalse(model.result["ok"])
        self.assertFalse((self.root / "bad.txt").exists())

    def test_write_requires_flag_in_implementation(self):
        for allow_write in [False, True]:
            workspace = Workspace(self.root, allow_write=allow_write)
            model = RecordingModel()
            Agent(model, workspace_tools(workspace), mode="implement").run("Inspect")
            names = {spec.name for spec in model.calls[0][1]}
            self.assertEqual("write_file" in names, allow_write)

    def test_successful_turns_follow_up_and_plan_handoff(self):
        model = RecordingModel("Plan: edit a function")
        state = ConversationState()
        Agent(model, workspace_tools(self.workspace), mode="plan").run("Original task", state)
        self.assertEqual(state.latest_plan, "Plan: edit a function")
        state.accepted_plan = state.latest_plan
        model.reply = "Implemented"
        result = Agent(model, workspace_tools(self.workspace), mode="implement",
                       project_instructions="Use Python").run("Now do it", state)
        messages = model.calls[-1][0]
        self.assertEqual([m.content for m in messages if m.role == "user"], ["Original task", "Now do it"])
        self.assertTrue(any("Plan: edit a function" in m.content and m.role == "system" for m in messages))
        self.assertTrue(any("Use Python" in m.content and m.role == "system" for m in messages))
        self.assertEqual([m.role for m in state.messages], ["user", "assistant", "user", "assistant"])
        self.assertEqual(state.messages, [m for m in result.messages if m.role != "system"])
        self.assertEqual(state.latest_plan, "Plan: edit a function")

    def test_failure_and_interrupt_do_not_commit_partial_turn(self):
        state = ConversationState(messages=[Message("user", "Old"), Message("assistant", "Done")],
                                  latest_plan="Existing plan", accepted_plan="Accepted")
        original = list(state.messages)
        root = self.root
        for error in [RuntimeError("network"), KeyboardInterrupt()]:
            class FailingModel:
                def __init__(self):
                    self.calls = 0

                def generate(self, messages, tools):
                    self.calls += 1
                    if self.calls == 1:
                        return ModelResponse(Message("assistant", tool_calls=(ToolCall(
                            "w", "write_file", '{"path":"changed.txt","content":"changed"}'),)))
                    raise error
            with self.subTest(error=type(error)), self.assertRaises(type(error)):
                Agent(FailingModel(), workspace_tools(self.workspace)).run("New", state)
            self.assertEqual(state.messages, original)
            self.assertEqual(state.latest_plan, "Existing plan")
            self.assertEqual(state.accepted_plan, "Accepted")
            self.assertEqual((root / "changed.txt").read_text(), "changed")

    def test_mode_is_recorded(self):
        output = StringIO()
        Agent(RecordingModel(), workspace_tools(self.workspace), JsonlEventSink(output),
              mode="plan").run("Inspect")
        self.assertEqual(json.loads(output.getvalue().splitlines()[0])["mode"], "plan")

    def test_instruction_loader_absent_root_only_and_snapshot(self):
        self.assertEqual(load_project_instructions(self.workspace).content, "")
        (self.root / "nested").mkdir()
        (self.root / "nested/AGENTS.md").write_text("Nested")
        (self.root / "AGENTS.md").write_text("Root rules")
        loaded = load_project_instructions(self.workspace)
        (self.root / "AGENTS.md").write_text("Changed")
        self.assertEqual(loaded.content, "Root rules")
        self.assertEqual(loaded.path, str(self.root / "AGENTS.md"))

    def test_invalid_instructions(self):
        path = self.root / "AGENTS.md"
        for value in [b'\xff', b'x' * 100001]:
            path.write_bytes(value)
            with self.assertRaises(ValueError):
                load_project_instructions(self.workspace)
        path.unlink()
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "AGENTS.md"
            target.write_text("outside")
            path.symlink_to(target)
            with self.assertRaises(ValueError):
                load_project_instructions(self.workspace)


class MultiTurnContextTests(unittest.TestCase):
    def test_pin_rules_plan_original_and_latest_without_duplicate_messages(self):
        fixed = [Message("system", "Runtime"), Message("system", "Project rules"),
                 Message("system", "Accepted plan")]
        old = [Message("user", "Original"), Message("assistant", "a" * 500)]
        middle = [Message("user", "Middle"), Message("assistant", "b" * 500)]
        active = [Message("user", "Latest"),
                  Message("assistant", tool_calls=(ToolCall("a", "read", '{}'), ToolCall("b", "read", '{}'))),
                  Message("tool", "one", call_id="a"), Message("tool", "two", call_id="b")]
        history = fixed + old + middle + active
        expected = fixed + [old[0]] + active
        budget = context_chars(expected, [])
        result = RuleBasedContextBuilder().build(history, [], budget)
        self.assertEqual(result.messages, tuple(expected))
        self.assertEqual(result.stats.dropped_rounds, 2)
        self.assertEqual(result.stats.selected_chars, budget)
        self.assertEqual(history, fixed + old + middle + active)
        with self.assertRaises(ContextLimitError):
            RuleBasedContextBuilder("full").build(history, [], budget)

    def test_completed_turns_are_optional_whole_units(self):
        messages = [Message("system", "Rules"), Message("user", "Original"),
                    Message("assistant", "a" * 500), Message("user", "Follow-up"),
                    Message("assistant", "Follow-up answer"), Message("user", "Current")]
        expected = [messages[0], messages[1], *messages[3:]]
        selection = RuleBasedContextBuilder().build(messages, [], context_chars(expected, []))
        self.assertEqual(selection.messages, tuple(expected))
        self.assertEqual(selection.stats.dropped_rounds, 1)

    def test_unfinished_previous_turn_is_rejected(self):
        for messages in [[Message("user", "one"), Message("user", "two")],
                         [Message("user", "one"), Message("assistant", tool_calls=(ToolCall("a", "x", '{}'),)),
                          Message("tool", "result", call_id="a"), Message("user", "two")]]:
            with self.assertRaises(ValueError):
                RuleBasedContextBuilder().build(messages, [], 10000)
