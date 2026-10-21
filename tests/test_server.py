import io
import json
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent.adapters.execution import LocalCommandRunner
from agent.adapters.models import OpenAICompatibleProvider, OpenAIProvider
from agent.core.cancellation import CancellationToken, RunCancelled
from agent.core.engine import Agent
from agent.core.types import ConversationState, Message, ModelResponse, ToolCall
from agent.server import EditorTools, Server, restore_state
from agent.files import Workspace


class ServerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        storage = tempfile.TemporaryDirectory()
        self.addCleanup(storage.cleanup)
        env = patch.dict('os.environ', {'AGENT_STATE_DIR': storage.name})
        env.start()
        self.addCleanup(env.stop)
        self.output = io.StringIO()
        self.server = Server(self.output)
        self.addCleanup(self.server.close)
        self.config = {'protocolVersion': 1, 'workspace': str(self.root), 'provider': 'demo'}
        self.request(1, 'initialize', self.config)

    def request(self, request_id, method, params=None):
        self.server.handle({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params or {}})

    def frames(self):
        with self.server._write_lock:
            return [json.loads(line) for line in self.output.getvalue().splitlines()]

    def wait(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            for frame in self.frames():
                if predicate(frame):
                    return frame
            time.sleep(0.005)
        self.fail('Expected protocol frame was not produced.')

    def reply(self, request, value):
        self.server.handle({'jsonrpc': '2.0', 'id': request['id'], 'result': value})

    def test_demo_round_trip_through_editor_without_disk_writes(self):
        self.request(2, 'session/run', {'task': 'Inspect', 'mode': 'plan'})
        self.reply(self.wait(lambda f: f.get('method') == 'editor/instructions'), 'Use small edits')
        listing = self.wait(lambda f: f.get('method') == 'editor/tool')
        self.assertEqual(listing['params']['name'], 'list_files')
        self.reply(listing, {'ok': True, 'content': 'hello.py'})
        result = self.wait(lambda f: f.get('id') == 2)['result']
        self.assertIn('hello.py', result['text'])
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(restore_state(result['checkpoint']).messages[-1].role, 'assistant')
        self.assertTrue(any(f.get('method') == 'agent/event' for f in self.frames()))

    def test_cancel_releases_pending_approval_and_preserves_checkpoint(self):
        self.request(2, 'session/run', {'task': 'Inspect', 'mode': 'implement'})
        self.wait(lambda f: f.get('method') == 'editor/instructions')
        self.request(3, 'cancel')
        reply = self.wait(lambda f: f.get('id') == 3)['result']
        self.assertTrue(reply['cleanupComplete'])
        self.assertEqual(self.wait(lambda f: f.get('id') == 2)['error']['code'], -32800)
        self.assertEqual(self.server.state.messages, [])

    def test_busy_unknown_and_incompatible_configuration(self):
        self.request(2, 'session/run', {'task': 'Inspect', 'mode': 'plan'})
        self.wait(lambda f: f.get('method') == 'editor/instructions')
        self.request(3, 'session/reset')
        self.assertEqual(self.wait(lambda f: f.get('id') == 3)['error']['code'], -32001)
        self.request(4, 'cancel')
        self.server.worker.join(1)
        self.request(5, 'unknown')
        self.assertEqual(self.wait(lambda f: f.get('id') == 5)['error']['code'], -32601)
        for i, config in enumerate([
                {**self.config, 'protocolVersion': 2},
                {**self.config, 'checks': {'lint_fix': {'command': ['bad']}}},
                {**self.config, 'checks': {'test': {'command': ['x'], 'timeout': float('nan')}}},
                {**self.config, 'maxSteps': True}], 10):
            self.request(i, 'initialize', config)
            self.assertEqual(self.wait(lambda f: f.get('id') == i)['error']['code'], -32602)

    def test_provider_exception_does_not_expose_secret(self):
        self.server.provider_factory = Mock(side_effect=RuntimeError('private-api-key-value'))
        self.request(2, 'session/run', {'task': 'Inspect', 'mode': 'plan'})
        self.reply(self.wait(lambda f: f.get('method') == 'editor/instructions'), '')
        error = self.wait(lambda f: f.get('id') == 2)['error']
        self.assertEqual(error['code'], -32000)
        self.assertNotIn('private-api-key-value', self.output.getvalue())

    def test_completion_has_no_tools_and_preserves_session(self):
        class Model:
            def generate(inner, messages, tools):
                self.assertEqual(tools, [])
                self.assertIn('x<CURSOR>y', messages[1].content)
                return ModelResponse(Message('assistant', ' + 1'))
        factory = Mock(return_value=Model())
        self.server.provider_factory = factory
        self.server.config['provider'] = 'ollama'
        before = asdict(self.server.state)
        self.request(2, 'completion/generate', {'prefix': 'x', 'suffix': 'y', 'language': 'python'})
        self.assertEqual(self.wait(lambda f: f.get('id') == 2)['result']['text'], ' + 1')
        factory.assert_called_once_with(self.server.config, completion=True)
        self.assertEqual(asdict(self.server.state), before)

    def test_invalid_checkpoint_rejected(self):
        for value in ({}, {'messages': [], 'latest_plan': 7, 'accepted_plan': None},
                      {'messages': [asdict(Message('user', 'unfinished'))], 'latest_plan': None, 'accepted_plan': None}):
            with self.assertRaises(ValueError):
                restore_state(value)

    def test_editor_path_guards_and_command_permission(self):
        bridge = Mock(return_value=False)
        tools = EditorTools(Workspace(self.root, True), bridge,
                            {'test': {'command': [sys.executable, '-c', 'pass']}}, CancellationToken())
        self.assertFalse(tools.execute(ToolCall('x', 'read_file', '{"path":"../outside"}')).ok)
        bridge.assert_not_called()
        self.assertFalse(tools.execute(ToolCall('x', 'run_tests', '{"command":"evil"}')).ok)
        bridge.assert_not_called()
        result = tools.execute(ToolCall('x', 'run_tests', '{}'))
        self.assertFalse(result.ok)
        self.assertTrue(json.loads(result.content)['not_run'])


class CancellationTests(unittest.TestCase):
    def test_late_model_response_cannot_commit_or_execute(self):
        token = CancellationToken()
        class Model:
            def generate(self, messages, tools):
                token.cancel()
                return ModelResponse(Message('assistant', 'late'))
        tools = Mock()
        tools.specifications.return_value = ()
        state = ConversationState()
        with self.assertRaises(RunCancelled):
            Agent(Model(), tools, cancellation=token).run('task', state)
        self.assertEqual(state.messages, [])
        tools.execute.assert_not_called()

    def test_command_cancel_stops_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as directory:
            token = CancellationToken()
            runner = LocalCommandRunner(Path(directory), [sys.executable, '-c', 'import time; time.sleep(30)'], cancellation=token)
            result = []
            thread = threading.Thread(target=lambda: result.append(runner.run()))
            thread.start()
            deadline = time.monotonic() + 3
            while not token._processes and time.monotonic() < deadline:
                time.sleep(0.005)
            token.cancel()
            self.assertTrue(token.wait_for_cleanup(3))
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertFalse(result[0]['passed'])
            self.assertTrue(result[0]['cancelled'])
            self.assertIsNotNone(result[0]['exit_code'])

    def test_cancel_before_spawn_creates_no_process(self):
        token = CancellationToken()
        token.cancel()
        with tempfile.TemporaryDirectory() as directory, patch('agent.adapters.execution.subprocess.Popen') as spawn:
            with self.assertRaises(RunCancelled):
                LocalCommandRunner(Path(directory), ['not-run'], cancellation=token).run()
            spawn.assert_not_called()

    def test_output_limits_are_translated_by_provider(self):
        client = Mock()
        client.responses.create.return_value = SimpleNamespace(output=[], output_text='x', usage=None)
        OpenAIProvider('model', client=client, max_output_tokens=256).generate([], [])
        self.assertEqual(client.responses.create.call_args.kwargs['max_output_tokens'], 256)
        client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='x', tool_calls=[]))], usage=None)
        OpenAICompatibleProvider('model', 'http://localhost:1234/v1', 'test', client=client,
                                 max_output_tokens=256).generate([], [])
        self.assertEqual(client.chat.completions.create.call_args.kwargs['max_tokens'], 256)
