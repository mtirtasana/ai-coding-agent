import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent.adapters.models import DemoProvider, OllamaProvider, OpenAIProvider
from agent.core.types import Message, ModelUsage


class UsageTests(unittest.TestCase):
    def openai(self, **fields):
        response = SimpleNamespace(output=[], output_text="Done", **fields)
        client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kwargs: response))
        return OpenAIProvider("test", client).generate([Message("user", "Task")], [])

    def test_openai_reports_actual_usage_including_zero(self):
        result = self.openai(usage=SimpleNamespace(input_tokens=100, output_tokens=0,
                            input_tokens_details=SimpleNamespace(cached_tokens=40)))
        self.assertEqual(result.usage, ModelUsage(100, 0, 40))

    def test_unknown_openai_usage_and_partial_details(self):
        self.assertIsNone(self.openai().usage)
        self.assertIsNone(self.openai(usage=None).usage)
        self.assertEqual(self.openai(usage=SimpleNamespace(input_tokens=5)).usage,
                         ModelUsage(input_tokens=5))
        self.assertEqual(self.openai(usage=SimpleNamespace(input_tokens=-1,
                         output_tokens=True)).usage, ModelUsage())

    def ollama(self, **fields):
        response = io.BytesIO(json.dumps({"message": {"content": "Done"}, **fields}).encode())
        with patch("urllib.request.urlopen", return_value=response):
            return OllamaProvider("test").generate([Message("user", "Task")], [])

    def test_ollama_usage(self):
        self.assertEqual(self.ollama(prompt_eval_count=0, eval_count=12).usage, ModelUsage(0, 12))
        self.assertIsNone(self.ollama().usage)
        self.assertEqual(self.ollama(prompt_eval_count=5).usage, ModelUsage(input_tokens=5))

    def test_ollama_ids_remain_unique_when_selected_history_length_is_constant(self):
        ids = []
        for _ in range(2):
            payload = {"message": {"tool_calls": [{"function": {
                "name": "read_file", "arguments": {"path": "test.txt"}}}]}}
            response = io.BytesIO(json.dumps(payload).encode())
            with patch("urllib.request.urlopen", return_value=response):
                result = OllamaProvider("test").generate([Message("user", "Task")], [])
                ids.append(result.message.tool_calls[0].id)
        self.assertNotEqual(*ids)

    def test_demo_has_no_usage(self):
        self.assertIsNone(DemoProvider().generate([Message("user", "Task")], []).usage)
