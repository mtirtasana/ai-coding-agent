import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.adapters.events import JsonlEventSink
from agent.adapters.models import DemoProvider, OllamaProvider, OpenAIProvider
from agent.adapters.tools import workspace_tools
from agent.core.engine import Agent, ContextLimitError, StepLimitError
from agent.core.types import Message, ModelResponse, ToolCall
from agent.files import Workspace


class ArchitectureTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tools = workspace_tools(Workspace(Path(temp.name)))

    def test_offline_cycle_and_events(self):
        stream = StringIO()
        result = Agent(DemoProvider(), self.tools, JsonlEventSink(stream)).run("Inspect")
        self.assertEqual(result.steps, 2)
        self.assertEqual(result.messages[-2].role, "tool")
        self.assertTrue(json.loads(result.messages[-2].content)["ok"])
        self.assertEqual(json.loads(stream.getvalue().splitlines()[-1])["event"], "run_completed")

    def test_validation_and_read_only(self):
        for name, args in [("write_file", '{"path":"x","content":"y"}'),
                           ("read_file", '{"path":42}'),
                           ("read_file", '{"path":"x","extra":"x"}'),
                           ("read_file", 'invalid')]:
            with self.subTest(name=name, args=args):
                self.assertFalse(self.tools.execute(ToolCall("a", name, args)).ok)

    def test_writable_workspace_routes_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            tools = workspace_tools(Workspace(Path(directory), allow_write=True))
            written = tools.execute(ToolCall("w", "write_file",
                '{"path":"test.txt","content":"hello"}'))
            self.assertTrue(written.ok)
            read = tools.execute(ToolCall("r", "read_file", '{"path":"test.txt"}'))
            self.assertEqual(read.content, "hello")

    def test_ollama_tool_result_translation(self):
        import io
        response = io.BytesIO(b'{"message":{"content":"Done"}}')
        messages = [Message("assistant", tool_calls=(
            ToolCall("a", "read_file", '{"path":"test.txt"}'),)),
            Message("tool", '{"ok":true,"content":"hello"}', call_id="a")]
        with patch("urllib.request.OpenerDirector.open", return_value=response) as send:
            result = OllamaProvider("local").generate(messages, [])
            payload = json.loads(send.call_args.args[0].data)
        self.assertEqual(payload["messages"][-1]["tool_name"], "read_file")
        self.assertEqual(result.message.content, "Done")

    def test_error_reaches_model(self):
        class Model:
            def generate(self, messages, tools):
                if messages[-1].role == "user":
                    return ModelResponse(Message("assistant", tool_calls=(ToolCall("x", "unknown", '{}'),)))
                if json.loads(messages[-1].content)["ok"]:
                    raise AssertionError("Expected tool failure")
                return ModelResponse(Message("assistant", "Recovered"))
        self.assertEqual(Agent(Model(), self.tools).run("Task").text, "Recovered")

    def test_limits(self):
        with self.assertRaises(ContextLimitError):
            Agent(DemoProvider(), self.tools, max_context_chars=1).run("Task")
        with self.assertRaises(StepLimitError):
            Agent(DemoProvider(), self.tools, max_steps=1).run("Task")

    def test_runs_have_independent_context(self):
        agent = Agent(DemoProvider(), self.tools)
        self.assertEqual(len(agent.run("One").messages), len(agent.run("Two").messages))

    def test_openai_translation(self):
        captured = []
        def create(**kwargs):
            captured.append(kwargs)
            return SimpleNamespace(output_text="Done", output=[])
        provider = OpenAIProvider("test", SimpleNamespace(responses=SimpleNamespace(create=create)))
        messages = [Message("system", "rules"), Message("user", "task"),
                    Message("assistant", tool_calls=(ToolCall("a", "read_file", '{"path":"x"}'),)),
                    Message("tool", '{"ok":true}', call_id="a")]
        self.assertEqual(provider.generate(messages, self.tools.specifications()).message.content, "Done")
        self.assertEqual(captured[0]["input"][-1]["call_id"], "a")
        self.assertEqual(captured[0]["input"][-2]["type"], "function_call")
        self.assertFalse(captured[0]["store"])

    def test_openai_tool_response(self):
        response = SimpleNamespace(output_text="", output=[SimpleNamespace(
            type="function_call", call_id="a", name="list_files", arguments='{"path":"."}',
            model_dump=lambda **kwargs: {"type": "function_call", "call_id": "a",
                                         "name": "list_files", "arguments": '{"path":"."}'})])
        client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kwargs: response))
        self.assertEqual(OpenAIProvider("test", client).generate([], []).message.tool_calls[0].name,
                         "list_files")

    def test_ollama_translation(self):
        import io
        response = io.BytesIO(json.dumps({"message": {"content": "", "tool_calls": [
            {"function": {"name": "list_files", "arguments": {"path": "."}}}]}}).encode())
        with patch("urllib.request.OpenerDirector.open", return_value=response) as send:
            result = OllamaProvider("local").generate([Message("user", "task")],
                                                       self.tools.specifications())
            payload = json.loads(send.call_args.args[0].data)
        self.assertFalse(payload["stream"])
        self.assertEqual(result.message.tool_calls[0].name, "list_files")
        self.assertEqual(json.loads(result.message.tool_calls[0].arguments), {"path": "."})


if __name__ == "__main__":
    unittest.main()
