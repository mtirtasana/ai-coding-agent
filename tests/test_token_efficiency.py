import json
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent.adapters.check_output import CheckOutputStore
from agent.adapters.execution import LocalCommandRunner
from agent.adapters.tokens import ModelTokenEstimator
from agent.adapters.tools import ToolRegistry, workspace_tools
from agent.core.context import ContextLimitError, RuleBasedContextBuilder, split_turns
from agent.core.engine import Agent, TokenBudgetError
from agent.core.tokens import ByteTokenEstimator, TokenEstimate
from agent.core.types import ConversationState, Message, ModelResponse, ModelUsage, ToolCall, ToolResult
from agent.files import Workspace
from agent.server import EditorTools, Server
from agent.core.cancellation import CancellationToken


class OutputTests(unittest.TestCase):
    def report(self, check='test', passed=True, output=None):
        return {'check': check, 'passed': passed, 'exit_code': 0 if passed else 1,
                'output': output or 'START\n' + 'detail\n' * 2000 + 'END\n',
                'capture_complete': True, 'output_truncated': False}

    def test_excerpt_and_retrieval_preserve_metadata_and_original(self):
        store = CheckOutputStore()
        original = self.report()
        result = store.present(ToolResult(True, json.dumps(original)))
        excerpt = json.loads(result.content)
        self.assertLess(len(result.content), len(json.dumps(original)) // 2)
        self.assertTrue(excerpt['output_excerpt'])
        self.assertFalse(excerpt['output_truncated'])
        self.assertEqual(excerpt['exit_code'], original['exit_code'])
        lines = json.loads(store.read(excerpt['output_id'], 1, 3))
        self.assertEqual(lines['content'], 'START\ndetail\ndetail\n')
        self.assertEqual(original['output'].splitlines()[0], 'START')
        with self.assertRaises(ValueError):
            store.read(excerpt['output_id'], 1, 201)

    def test_failures_include_head_and_tail_scanners_stay_verbatim(self):
        store = CheckOutputStore()
        original = self.report(passed=False)
        failure = json.loads(store.present(ToolResult(False, json.dumps(original))).content)
        self.assertIn('START', failure['output'])
        self.assertIn('END', failure['output'])
        self.assertFalse(failure['passed'])
        for check in ('security', 'dependency_audit'):
            report = self.report(check=check)
            self.assertEqual(json.loads(store.present(ToolResult(True, json.dumps(report))).content), report)

    def test_logs_expire_without_rerunning_and_oversized_logs_are_not_omitted(self):
        store = CheckOutputStore(max_entries=1)
        first = json.loads(store.present(ToolResult(True, json.dumps(self.report()))).content)['output_id']
        second = json.loads(store.present(ToolResult(True, json.dumps(self.report()))).content)['output_id']
        with self.assertRaisesRegex(ValueError, 'expired'):
            store.read(first, 1, 1)
        self.assertEqual(json.loads(store.read(second, 1, 1))['content'], 'START\n')
        report = self.report(output='x' * 3000)
        self.assertEqual(json.loads(CheckOutputStore(max_chars=2000).present(
            ToolResult(True, json.dumps(report))).content), report)

    def test_configured_tool_exposes_reader_and_does_not_reexecute(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            runner = LocalCommandRunner(workspace.root, [sys.executable, '-c', "print('detail\\n' * 2000)"])
            tools = workspace_tools(workspace, test_runner=runner)
            with patch.object(runner, 'run', wraps=runner.run) as run:
                check = json.loads(tools.execute(ToolCall('a', 'run_tests', '{}')).content)
                reader = tools.execute(ToolCall('b', 'read_check_output', json.dumps(
                    {'output_id': check['output_id'], 'start_line': 1, 'end_line': 2})))
                self.assertTrue(reader.ok)
                self.assertEqual(json.loads(reader.content)['content'], 'detail\ndetail\n')
                self.assertEqual(run.call_count, 1)

    def test_editor_receives_full_check_output_model_receives_excerpt(self):
        with tempfile.TemporaryDirectory() as directory:
            reports = []
            def bridge(method, params):
                if method == 'editor/beforeCheck':
                    return True
                if method == 'editor/checkResult':
                    reports.append(params['result'])
            workspace = Workspace(Path(directory), True)
            tools = EditorTools(workspace, bridge, {'test': {'command': [sys.executable, '-c',
                                "print('detail\\n' * 2000)"]}}, CancellationToken())
            check = tools.execute(ToolCall('a', 'run_tests', '{}'))
            excerpt = json.loads(check.content)
            self.assertGreater(len(reports[0]['output']), len(excerpt['output']))
            self.assertTrue(excerpt['passed'])
            self.assertTrue(tools.execute(ToolCall('b', 'read_check_output', json.dumps(
                {'output_id': excerpt['output_id'], 'start_line': 1, 'end_line': 2}))).ok)


class CompactionTests(unittest.TestCase):
    def history(self):
        messages = [Message('system', 'Rules and accepted plan'), Message('user', 'Original request')]
        for index in range(5):
            call = ToolCall(str(index), 'file_snapshot', '{"path":"a.py"}')
            payload = {'path': 'a.py', 'sha256': 'a' * 64, 'content': 'source data\n' * 1000}
            messages.extend([Message('assistant', 'Decision retained', (call,)),
                             Message('tool', json.dumps({'ok': True, 'content': json.dumps(payload)}), call_id=str(index))])
        return messages

    def test_references_reduce_context_preserve_protocol_and_latest_rounds(self):
        history = self.history()
        original = list(history)
        recent = RuleBasedContextBuilder().build(history, [], 200000)
        compact = RuleBasedContextBuilder('compact').build(history, [], 200000)
        self.assertLess(compact.stats.selected_chars, recent.stats.selected_chars * 0.6)
        self.assertEqual(history, original)
        self.assertEqual(compact.messages[:2], tuple(history[:2]))
        self.assertEqual(compact.messages[-4:], tuple(history[-4:]))
        self.assertEqual(split_turns(compact.messages), split_turns(history))
        for message in compact.messages[2:-4]:
            if message.role == 'assistant':
                self.assertEqual(message.content, 'Decision retained')
            else:
                reference = json.loads(json.loads(message.content)['content'])
                self.assertEqual(reference['sha256'], 'a' * 64)
                self.assertTrue(reference['content_omitted'])
                self.assertIn('stale', reference['note'])
        self.assertEqual(compact.stats.full_chars, recent.stats.full_chars)

    def test_errors_arbitrary_payloads_and_full_mode_are_not_compacted(self):
        history = self.history()
        history[3] = Message('tool', json.dumps({'ok': False, 'content': 'error' * 1500}), call_id='0')
        history[5] = Message('tool', '{"ok":true,"content":"[]"}', call_id='1')
        compact = RuleBasedContextBuilder('compact').build(history, [], 200000)
        self.assertEqual(compact.messages[3], history[3])
        self.assertEqual(compact.messages[5], history[5])
        self.assertEqual(RuleBasedContextBuilder('full').build(history, [], 200000).messages, tuple(history))


class BudgetTests(unittest.TestCase):
    def model(self, usage=None):
        model = Mock()
        model.generate.return_value = ModelResponse(Message('assistant', 'Done'), usage)
        return model

    def test_context_reserves_output_and_stops_before_provider(self):
        model = self.model()
        with self.assertRaises(ContextLimitError):
            Agent(model, ToolRegistry(), max_context_tokens=100, output_token_reserve=100).run('Task')
        model.generate.assert_not_called()

    def test_run_budget_stops_before_provider_and_preserves_state(self):
        model, state = self.model(), ConversationState()
        with self.assertRaises(TokenBudgetError):
            Agent(model, ToolRegistry(), max_run_tokens=100, output_token_reserve=100).run('Task', state)
        model.generate.assert_not_called()
        self.assertEqual(state.messages, [])

    def test_known_usage_and_unknown_usage_are_separate_from_estimates(self):
        for usage in (None, ModelUsage(12, 3, 10)):
            result = Agent(self.model(usage), ToolRegistry()).run('Task')
            request = result.requests[0]
            self.assertGreater(request.estimated_input_tokens, 100)
            self.assertEqual(request.usage, usage)
            self.assertEqual(request.token_estimate_method, 'utf8-bytes+margin')

    def test_cumulative_budget_reserves_next_output_and_uses_reported_counts(self):
        estimator = Mock()
        estimator.estimate.return_value = TokenEstimate(100, 'test')
        tools = ToolRegistry()
        from agent.adapters.tools import spec
        tools.register(spec('read', 'Read', {}), lambda: 'data')
        for usage, budget in [(ModelUsage(100, 10, 90), 219), (None, 219)]:
            model = Mock()
            model.generate.return_value = ModelResponse(Message('assistant', tool_calls=(
                ToolCall('a', 'read', '{}'),)), usage)
            with self.assertRaises(TokenBudgetError):
                Agent(model, tools, token_estimator=estimator, max_run_tokens=budget,
                      output_token_reserve=10).run('Task')
            self.assertEqual(model.generate.call_count, 1)

    def test_token_selection_drops_optional_rounds_without_breaking_pairs(self):
        messages = CompactionTests().history()
        state = ConversationState(messages=messages[1:] + [Message('assistant', 'Earlier answer')])
        model = self.model()
        estimator = Mock()
        estimator.estimate.side_effect = lambda messages, tools: TokenEstimate(len(messages) * 100, 'test')
        result = Agent(model, ToolRegistry(), token_estimator=estimator, max_context_tokens=1100,
                       output_token_reserve=100).run('Follow-up', state)
        sent = model.generate.call_args.args[0]
        self.assertLessEqual(len(sent), 10)
        expected = {call.id for message in sent for call in message.tool_calls}
        self.assertEqual({message.call_id for message in sent if message.role == 'tool'}, expected)
        self.assertEqual(sent[-1].content, 'Follow-up')
        self.assertEqual(result.requests[0].token_estimate_method, 'test')

    def test_optional_model_tokenizer_and_unknown_model_fallback(self):
        encoding = SimpleNamespace(name='fixture', encode=Mock(return_value=[1] * 10))
        with patch.dict(sys.modules, {'tiktoken': SimpleNamespace(encoding_for_model=lambda model: encoding,
                        encoding_name_for_model=lambda model: 'fixture')}):
            estimate = ModelTokenEstimator('openai', 'fixture').estimate([Message('user', '<|endoftext|>😀')], [])
            self.assertEqual(estimate.method, 'tiktoken:fixture+margin')
            self.assertEqual(encoding.encode.call_args.kwargs, {'disallowed_special': ()})
        with patch.dict(sys.modules, {'tiktoken': None}):
            estimate = ModelTokenEstimator('openai', 'unknown').estimate([Message('user', '😀')], [])
            self.assertEqual(estimate, ByteTokenEstimator().estimate([Message('user', '😀')], []))
        self.assertNotIn('input_tokens', asdict(estimate))

    def test_optional_asset_loading_never_blocks_requests_and_is_shared(self):
        import threading
        import time
        release = threading.Event()
        loader = Mock(side_effect=lambda model: release.wait(5) and None)
        module = SimpleNamespace(encoding_for_model=loader, encoding_name_for_model=lambda model: 'slow-fixture')
        try:
            with patch.dict(sys.modules, {'tiktoken': module}):
                first = ModelTokenEstimator('openai', 'model-a')
                second = ModelTokenEstimator('openai', 'model-b')
                started = time.monotonic()
                self.assertEqual(first.estimate([Message('user', 'Task')], []).method, 'utf8-bytes+margin')
                self.assertLess(time.monotonic() - started, 1)
                self.assertIs(first.pending, second.pending)
                self.assertEqual(loader.call_count, 1)
        finally:
            release.set()

    def test_server_settings_validation_and_run_budget(self):
        from io import StringIO
        with tempfile.TemporaryDirectory() as directory:
            server = Server(StringIO())
            base = {'protocolVersion': 2, 'workspace': directory, 'provider': 'demo'}
            for change in ({'maxRunTokens': True}, {'maxContextTokens': 0}, {'maxRunTokens': -1},
                           {'maxContextTokens': '1000'}, {'contextMode': 'unknown'}):
                with self.subTest(change=change), self.assertRaises(ValueError):
                    server.initialize({**base, **change})
            server.initialize({**base, 'maxRunTokens': 1, 'contextMode': 'compact'})
            server.editor = lambda *args: ''
            with self.assertRaises(TokenBudgetError):
                server.run({'task': 'Inspect', 'mode': 'plan'})
