import unittest
from types import SimpleNamespace as NS

from agent.adapters.models import OpenAICompatibleProvider
from agent.adapters.tools import spec
from agent.core.types import Message, ModelUsage, ToolCall


class CompatibleTests(unittest.TestCase):
    def provider(self, response):
        self.sent = []
        def create(**kwargs):
            self.sent.append(kwargs)
            return response
        return OpenAICompatibleProvider('test-model', 'http://localhost:1234/v1', 'test-key',
                                        client=NS(chat=NS(completions=NS(create=create))))

    def test_multi_round_translation_and_usage(self):
        response = NS(choices=[NS(message=NS(content=None, tool_calls=[NS(
            id='next', function=NS(name='read_file', arguments='{"path":"calc.py"}'))]))],
            usage=NS(prompt_tokens=20, completion_tokens=0, prompt_tokens_details=NS(cached_tokens=10)))
        provider = self.provider(response)
        messages = [Message('system', 'Rules'), Message('user', 'Task'),
                    Message('assistant', tool_calls=(ToolCall('old', 'read_file', '{"path":"x"}'),)),
                    Message('tool', 'result', call_id='old'), Message('user', 'Follow up')]
        result = provider.generate(messages, [spec('read_file', 'Read', {'path': {'type': 'string'}})])
        self.assertEqual(result.usage, ModelUsage(20, 0, 10))
        self.assertEqual(result.message.tool_calls[0].id, 'next')
        self.assertEqual(self.sent[0]['messages'][2]['tool_calls'][0]['id'], 'old')
        self.assertEqual(self.sent[0]['messages'][3]['tool_call_id'], 'old')
        self.assertNotIn('strict', self.sent[0]['tools'][0]['function'])
        self.assertFalse(self.sent[0]['stream'])
        self.assertEqual(len(self.sent), 1)

    def test_no_usage_and_no_tools(self):
        result = self.provider(NS(choices=[NS(message=NS(content='Done', tool_calls=None))])).generate([], [])
        self.assertIsNone(result.usage)
        self.assertEqual(result.message.content, 'Done')
        self.assertNotIn('tools', self.sent[0])

    def test_no_fallback_after_provider_error(self):
        class API:
            def create(self, **kwargs):
                raise RuntimeError('unavailable')
        provider = OpenAICompatibleProvider('test', 'http://localhost/v1', 'test',
                                             client=NS(chat=NS(completions=API())))
        with self.assertRaises(RuntimeError):
            provider.generate([], [])

    def test_configuration(self):
        for url in ['file:///tmp/api', 'http://user:key@localhost/v1', 'https://host/v1?key=secret', 'https://host/#fragment']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                OpenAICompatibleProvider('test', url, 'test', client=object())
