import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent.adapters.connections import ActiveConnection, ConnectionController, ConnectionSetupError, OSVault, ProfileStore
from agent.adapters.usage import MeteredProvider, UsageJournal, UsageSession, format_usage, parse_usage, period_bounds
from agent.cli import main
from agent.core.engine import Agent
from agent.core.types import Message, ModelResponse, ModelUsage
from agent.adapters.tools import workspace_tools
from agent.files import Workspace
from agent.server import Server
from agent.terminal import InteractiveShell


class FakeVault:
    def __init__(self):
        self.values = {}

    def get(self, reference):
        if reference not in self.values:
            raise ConnectionSetupError('Key unavailable.')
        return self.values[reference]

    def set(self, reference, value):
        if not value:
            raise ConnectionSetupError('Key required.')
        self.values[reference] = value

    def delete(self, reference):
        self.values.pop(reference, None)


class StorageFixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        env = patch.dict('os.environ', {'AGENT_STATE_DIR': str(self.root / 'state'), 'AGENT_CONFIG_DIR': str(self.root / 'config')})
        env.start()
        self.addCleanup(env.stop)
        self.journal = UsageJournal()
        self.store = ProfileStore(vault=FakeVault())


class ConnectionTests(StorageFixture):
    def test_secret_prompt_uses_no_history_or_default(self):
        from agent.terminal import create_connection_prompt
        from prompt_toolkit.history import DummyHistory
        ordinary, private = Mock(), Mock()
        with patch('prompt_toolkit.PromptSession', side_effect=[ordinary, private]) as creator:
            ask = create_connection_prompt()
            ask('API key', 'must-not-prefill', secret=True)
        self.assertIsInstance(creator.call_args_list[1].kwargs['history'], DummyHistory)
        self.assertEqual(private.prompt.call_args.kwargs, {'default': '', 'is_password': True})
        ordinary.prompt.assert_not_called()
        ordinary.prompt.return_value = ''
        self.assertEqual(ask('Provider', 'ollama'), 'ollama')
        self.assertIn('[ollama]', ordinary.prompt.call_args.args[0])

    def test_profile_metadata_contains_references_not_keys_and_keys_rotate(self):
        active = self.store.save('native', 'openai', 'model', key='private-api-key')
        self.assertNotIn('private-api-key', self.store.path.read_text())
        self.assertNotIn('private-api-key', repr(active))
        old_ref = self.store.get('native').credential_ref
        self.assertEqual(self.store.activate(self.store.get()).api_key, 'private-api-key')
        self.store.save('native', 'openai', 'next', key='replacement-key')
        self.assertNotIn(old_ref, self.store.vault.values)
        self.assertEqual(self.store.activate(self.store.get()).model, 'next')
        self.store.remove('native')
        self.assertFalse(self.store.vault.values)
        self.assertIsNone(self.store.load()['default'])

    def test_ollama_needs_no_keychain_and_invalid_profiles_make_no_writes(self):
        vault = Mock(side_effect=AssertionError('not needed'))
        store = ProfileStore(vault=vault)
        store.save('local', 'ollama', 'local-model')
        self.assertEqual(store.activate(store.get()).base_url, 'http://localhost:11434')
        vault.set.assert_not_called()
        for name, provider, model, url in [('bad name', 'openai', 'm', ''),
                ('x', 'unknown', 'm', ''), ('x', 'ollama', '', ''),
                ('x', 'ollama', 'm', 'file:///private'), ('x', 'openai-compatible', 'm', 'https://key:secret@host')]:
            with self.subTest(name=name, provider=provider), self.assertRaises(ConnectionSetupError):
                store.save(name, provider, model, url, key='key')
        self.assertEqual(len(store.load()['profiles']), 1)

    def test_failed_configuration_save_rolls_back_new_secret_and_keeps_old_profile(self):
        self.store.save('old', 'openai', 'model', key='old-key')
        before, keys = self.store.path.read_bytes(), dict(self.store.vault.values)
        with patch.object(self.store, 'write', side_effect=OSError('disk failure')), self.assertRaises(OSError):
            self.store.save('old', 'openai', 'next', key='next-key')
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.vault.values, keys)
        version = self.store.load()
        self.store.set_default('old')
        with self.assertRaisesRegex(ConnectionSetupError, 'another process'):
            self.store.write(version, version['revision'])
        self.assertFalse(self.store.path.with_suffix('.lock').exists())

    def test_corrupt_or_new_configuration_schema_is_not_overwritten(self):
        self.store.path.parent.mkdir(parents=True)
        for value in [{'version': 99}, {'version': True, 'revision': 0, 'default': None, 'profiles': []},
                      {'version': 1, 'revision': 0, 'default': None, 'profiles': [{'api_key': 'do-not-print'}]}]:
            self.store.path.write_text(json.dumps(value))
            with self.assertRaises(ConnectionSetupError) as error:
                self.store.load()
            self.assertNotIn('do-not-print', str(error.exception))

    def test_plaintext_keyrings_and_locked_backend_errors_are_rejected(self):
        plaintext = SimpleNamespace(priority=10)
        with patch.dict(sys.modules, {'keyring': SimpleNamespace(get_keyring=lambda: plaintext)}):
            with self.assertRaisesRegex(ConnectionSetupError, 'No plaintext'):
                OSVault().set('ref', 'private-key')
        backend = type('Keyring', (), {'__module__': 'keyring.backends.SecretService', 'priority': 1,
                                     'get_password': Mock(side_effect=RuntimeError('private-key'))})()
        with patch.dict(sys.modules, {'keyring': SimpleNamespace(get_keyring=lambda: backend)}):
            with self.assertRaises(ConnectionSetupError) as error:
                OSVault().get('ref')
            self.assertNotIn('private-key', str(error.exception))

    def test_switch_starts_new_conversation_but_configuration_failure_preserves_state(self):
        self.store.save('local', 'ollama', 'model')
        model = Mock()
        model.generate.return_value = ModelResponse(Message('assistant', 'Plan'))
        connections = ConnectionController(lambda config: model, self.store)
        identity = UsageSession()
        shell = InteractiveShell(Workspace(self.workspace, True), lambda mode, rules: Agent(model, workspace_tools(Workspace(self.workspace, True))),
                                 output=lambda text: None, connections=connections, usage=self.journal, identity=identity)
        shell.submit('old task')
        old = identity.id
        shell.draft = 'old draft'
        with patch('agent.terminal.create_connection_prompt', return_value=lambda *args, **kwargs: 'n'):
            shell.handle('/connect local')
        self.assertNotEqual(identity.id, old)
        self.assertEqual(shell.state.messages, [])
        self.assertEqual(shell.draft, '')
        self.assertEqual(shell.mode, 'plan')
        self.assertEqual(model.generate.call_count, 1)
        current = identity.id
        with patch('agent.terminal.create_connection_prompt', return_value=lambda *args, **kwargs: 'n'):
            shell.handle('/connect missing')
        self.assertEqual(identity.id, current)
        shell.handle('/providers')
        shell.handle('/stats')
        self.assertEqual(model.generate.call_count, 1)

    def test_wizard_cancellation_and_sdk_failure_leave_profile_and_connection_unchanged(self):
        old = ActiveConnection('demo', 'demo')
        creator = Mock(side_effect=RuntimeError('SDK unavailable'))
        controller = ConnectionController(creator, self.store, old)
        answers = iter(['add', 'local', 'ollama', 'model', 'http://localhost:11434', 'n'])
        with self.assertRaises(RuntimeError):
            controller.select('', lambda *args, **kwargs: next(answers), lambda text: None)
        self.assertEqual(controller.current, old)
        self.assertFalse(self.store.path.exists())
        answers = iter(['add', 'local', 'ollama', 'model', 'http://localhost:11434'])
        def cancelled(*args, **kwargs):
            try:
                return next(answers)
            except StopIteration:
                raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            controller.select('', cancelled, lambda text: None)
        self.assertFalse(self.store.path.exists())


class UsageTests(StorageFixture):
    def call(self, session='one', journal=None, usage=ModelUsage(10, 2, 3), interface='cli', kind='agent'):
        journal = journal or self.journal
        model = Mock()
        model.generate.return_value = ModelResponse(Message('assistant', 'private response'), usage)
        MeteredProvider(model, journal, session, provider_name='openai', model='model', interface=interface, kind=kind).generate(
            [Message('user', 'private prompt')], [])

    def test_sessions_frontends_categories_and_partial_tokens_are_separate(self):
        self.call()
        self.call(usage=ModelUsage(0, None, None))
        self.call(session='two', interface='vscode', kind='completion', usage=ModelUsage(7, 1))
        result = self.journal.query({'period': 'session'}, 'one')
        self.assertEqual(result['totals']['calls'], 2)
        self.assertEqual(result['totals']['tokens']['input_tokens'], {'reported_sum': 10, 'responses_with_value': 2})
        self.assertEqual(result['totals']['tokens']['output_tokens']['responses_with_value'], 1)
        self.assertEqual(self.journal.query({'period': 'all', 'interface': 'vscode'})['totals']['calls'], 1)
        self.assertEqual(self.journal.query({'period': 'all', 'kind': 'completion'})['groups'][0]['kind'], 'completion')
        fresh = UsageJournal()
        self.assertEqual(fresh.query({'period': 'all'})['totals']['calls'], 3)
        with closing_sqlite(self.journal.path) as connection:
            text = repr(connection.execute('SELECT * FROM calls').fetchall())
            self.assertNotIn('private prompt', text)
            self.assertNotIn('private response', text)

    def test_usage_is_recorded_before_late_cancellation_or_filtered_response(self):
        from agent.core.cancellation import CancellationToken, RunCancelled
        token = CancellationToken()
        model = Mock()
        def response(messages, tools):
            token.cancel()
            return ModelResponse(Message('assistant', 'discarded'), ModelUsage(5, 1))
        model.generate.side_effect = response
        wrapper = MeteredProvider(model, self.journal, 'one', provider_name='openai', model='model')
        with self.assertRaises(RunCancelled):
            Agent(wrapper, workspace_tools(Workspace(self.workspace)), cancellation=token).run('task')
        self.assertEqual(self.journal.query({'period': 'all'})['totals']['tokens']['input_tokens']['reported_sum'], 5)

    def test_failures_and_crashed_attempts_remain_unknown_instead_of_zero(self):
        model = Mock()
        model.generate.side_effect = TimeoutError('private endpoint')
        wrapper = MeteredProvider(model, self.journal, 'one', provider_name='openai', model='model')
        with self.assertRaises(TimeoutError):
            wrapper.generate([], [])
        self.journal.start('one', 'run', 'openai', 'ad-hoc', 'model', 'vscode', 'completion')
        result = UsageJournal().query({'period': 'all'})['totals']
        self.assertEqual((result['calls'], result['failed'], result['unfinished']), (2, 1, 1))
        self.assertIsNone(result['tokens']['input_tokens']['reported_sum'])

    def test_duplicate_updates_and_concurrent_workers_do_not_double_count(self):
        def worker(index):
            journal = UsageJournal()
            for _ in range(5):
                self.call(session=str(index), journal=journal)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(worker, range(2)))
        result = self.journal.query({'period': 'all'})['totals']
        self.assertEqual(result['calls'], 10)
        self.assertEqual(result['tokens']['input_tokens']['reported_sum'], 100)
        request = self.journal.start('one', 'run', 'openai', 'ad-hoc', 'model', 'cli', 'agent')
        self.journal.finish(request, ModelUsage(1, 0))
        self.journal.finish(request, ModelUsage(1, 0))
        self.assertEqual(self.journal.query({'period': 'all'})['totals']['calls'], 11)

    def test_read_queries_do_not_create_history_and_unknown_schema_is_preserved(self):
        self.assertIn('No usage recorded', format_usage(self.journal.query({'period': 'all'})))
        self.assertFalse(self.journal.path.exists())
        self.journal.path.parent.mkdir(parents=True)
        with closing_sqlite(self.journal.path) as connection:
            connection.execute('PRAGMA user_version=99')
        report = self.journal.query({'period': 'all'})
        self.assertTrue(report['storage_incomplete'])
        with closing_sqlite(self.journal.path) as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 99)

    def test_storage_failure_keeps_in_memory_totals_and_deduplicates_old_disk_rows(self):
        warnings = []
        self.journal.warn = warnings.append
        request = self.journal.start('one', 'run', 'openai', 'ad-hoc', 'model', 'cli', 'agent')
        connect = self.journal.connect
        def failure(write=False):
            if write:
                raise sqlite3.OperationalError('database is locked')
            return connect()
        with patch.object(self.journal, 'connect', side_effect=failure):
            self.journal.finish(request, ModelUsage(8, 2))
            report = self.journal.query({'period': 'all'})
        self.assertEqual(report['totals']['calls'], 1)
        self.assertEqual(report['totals']['unfinished'], 0)
        self.assertEqual(report['totals']['tokens']['input_tokens']['reported_sum'], 8)
        self.assertTrue(report['storage_incomplete'])
        self.assertEqual(len(warnings), 1)

    def test_periods_custom_dates_and_filters_reject_invalid_input(self):
        self.assertEqual(period_bounds('7d', now=2000000), (2000000 - 7 * 86400, 2000000))
        first, last = period_bounds('custom', '2026-01-01', '2026-01-31')
        self.assertEqual(datetime.fromtimestamp(first).date().isoformat(), '2026-01-01')
        self.assertEqual(datetime.fromtimestamp(last).date().isoformat(), '2026-02-01')
        for period in ['today', 'week', 'month']:
            start, end = period_bounds(period, now=1800000000)
            self.assertEqual(datetime.fromtimestamp(start).hour, 0)
            self.assertEqual(end, 1800000000)
        for text in ['bad', '--from 2026-01-01', 'month --from 2026-01-01 --to 2026-02-01', '--interface unknown']:
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.journal.query(parse_usage(text), 'one')
        with self.assertRaises(ValueError):
            period_bounds('custom', '20260101', '20260102')
        self.assertEqual(parse_usage('--from 2026-01-01 --to 2026-01-31')['period'], 'custom')

    @unittest.skipUnless(hasattr(time, 'tzset'), 'OS timezone switching requires tzset')
    def test_dst_calendar_day_and_monday_month_boundaries(self):
        old = os.environ.get('TZ')
        try:
            os.environ['TZ'] = 'EST5EDT,M3.2.0,M11.1.0'
            time.tzset()
            first, last = period_bounds('custom', '2026-03-08', '2026-03-08')
            self.assertEqual(last - first, 23 * 3600)
            first, last = period_bounds('custom', '2026-11-01', '2026-11-01')
            self.assertEqual(last - first, 25 * 3600)
            now = datetime(2026, 3, 11, 12).timestamp()
            self.assertEqual(datetime.fromtimestamp(period_bounds('week', now=now)[0]).date().isoformat(), '2026-03-09')
            self.assertEqual(datetime.fromtimestamp(period_bounds('month', now=now)[0]).day, 1)
        finally:
            if old is None:
                os.environ.pop('TZ', None)
            else:
                os.environ['TZ'] = old
            time.tzset()


class CliAndServerTests(StorageFixture):
    def test_legacy_long_model_label_does_not_block_inference(self):
        model = Mock()
        model.generate.return_value = ModelResponse(Message('assistant', 'done'), ModelUsage(1, 1))
        provider = MeteredProvider(model, self.journal, 'session', provider_name='ollama', model='x' * 300)
        provider.generate([], [])
        label = self.journal.query({'period': 'all'})['groups'][0]['model']
        self.assertEqual(len(label), 256)
        model.generate.assert_called_once()

    def invoke(self, arguments):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['coding-agent', *arguments]), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            main()
        return out.getvalue(), err.getvalue()

    def test_standalone_stats_needs_neither_workspace_nor_credentials(self):
        with patch('agent.cli.OpenAIProvider') as creator:
            out, _ = self.invoke(['--stats', 'all', '--workspace', '/does-not-exist'])
        self.assertIn('No usage recorded', out)
        creator.assert_not_called()
        self.invoke(['--stats', '--from', '2026-01-01', '--to', '2026-01-02'])
        for args in [ ['--stats', 'today', '--from', '2026-01-01', '--to', '2026-01-02'], ['--kind', 'completion'] ]:
            with self.assertRaises(SystemExit) as error:
                self.invoke(args)
            self.assertEqual(error.exception.code, 2)

    def test_startup_without_credentials_allows_exit_without_initializing_sdk(self):
        prompt = Mock()
        prompt.prompt.side_effect = ['/stats', '/providers', '/exit']
        with patch.dict('os.environ', {'OPENAI_API_KEY': ''}), \
                patch('agent.terminal.create_prompt_session', return_value=prompt), patch('agent.cli.OpenAIProvider') as creator:
            self.invoke(['--workspace', str(self.workspace)])
        creator.assert_not_called()

    def test_named_profile_is_complete_and_cannot_mix_overrides(self):
        self.store.save('local', 'ollama', 'saved-model')
        model = Mock()
        model.generate.return_value = ModelResponse(Message('assistant', 'Done'), ModelUsage(3, 1))
        with patch('agent.cli.OllamaProvider', return_value=model) as creator:
            self.invoke(['task', '--profile', 'local', '--workspace', str(self.workspace)])
        creator.assert_called_once_with('saved-model', 'http://localhost:11434')
        self.assertEqual(self.journal.query({'period': 'all'})['groups'][0]['profile'], 'local')
        with self.assertRaises(SystemExit) as error:
            self.invoke(['task', '--profile', 'local', '--provider', 'demo'])
        self.assertEqual(error.exception.code, 2)

    def test_usage_query_before_initialize_and_while_worker_is_busy(self):
        self.call = UsageTests.call.__get__(self)
        self.call(session='conversation', interface='vscode')
        output = io.StringIO()
        server = Server(output)
        server.usage = self.journal
        server.worker = SimpleNamespace(is_alive=lambda: True)
        server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'usage/query', 'params': {'period': 'session', 'sessionId': 'conversation'}})
        result = json.loads(output.getvalue())['result']
        self.assertEqual(result['totals']['calls'], 1)
        self.assertEqual(result, self.journal.query({'period': 'session'}, 'conversation'))

    def test_filtered_completion_and_late_cancellation_keep_known_usage(self):
        model = Mock()
        model.generate.return_value = ModelResponse(Message('assistant', '```python\npass\n```'), ModelUsage(11, 4))
        server = Server(io.StringIO(), provider_factory=lambda *args, **kwargs: model)
        server.initialize({'protocolVersion': 2, 'workspace': str(self.workspace), 'provider': 'ollama', 'model': 'local'})
        identity = server.session_id
        self.assertEqual(server.complete({'prefix': '', 'suffix': '', 'language': 'python'})['text'], '')
        server.token.cancel()
        with self.assertRaises(Exception):
            server.complete({'prefix': '', 'suffix': '', 'language': 'python'})
        report = server.usage.query({'period': 'session'}, identity)
        self.assertEqual(report['totals']['responses'], 2)
        self.assertEqual(report['totals']['tokens']['input_tokens']['reported_sum'], 22)
        self.assertEqual(report['groups'][0]['kind'], 'completion')
        server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'session/reset'})
        self.assertNotEqual(identity, server.session_id)
        self.assertEqual(server.usage.query({'period': 'session'}, server.session_id)['totals']['calls'], 0)
        self.assertEqual(server.usage.query({'period': 'all'})['totals']['calls'], 2)

    def test_sibling_worker_volatile_metadata_replaces_pending_database_row(self):
        request = self.journal.start('conversation', 'run', 'ollama', 'editor', 'local', 'vscode', 'completion')
        with patch.object(self.journal, 'connect', side_effect=sqlite3.OperationalError('locked')):
            self.journal.finish(request, ModelUsage(8, 2))
        pending = self.journal.volatile_calls()
        report_worker = UsageJournal()
        report = report_worker.query({'period': 'all'}, volatile_calls=pending + pending)
        self.assertEqual(report['totals']['calls'], 1)
        self.assertEqual(report['totals']['responses'], 1)
        self.assertEqual(report['totals']['unfinished'], 0)
        self.assertEqual(report['totals']['tokens']['input_tokens']['reported_sum'], 8)
        self.assertTrue(report['storage_incomplete'])
        self.assertEqual(report_worker.volatile_calls(), [])
        with self.assertRaises(ValueError):
            report_worker.query({'period': 'all'}, volatile_calls=[{**pending[0], 'prompt': 'must not be accepted'}])
        with patch.object(Path, 'exists', side_effect=PermissionError('unreadable')):
            self.assertTrue(report_worker.query({'period': 'all'})['storage_incomplete'])


@contextlib.contextmanager
def closing_sqlite(path):
    connection = sqlite3.connect(path)
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()
