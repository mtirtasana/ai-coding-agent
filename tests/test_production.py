import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

from agent.adapters.diagnostics import diagnose, export_report
from agent.adapters.execution import LocalCommandRunner
from agent.adapters.file_policy import FilePolicy
from agent.adapters.recovery import RecoveryStore
from agent.adapters.tools import workspace_tools
from agent.adapters.usage import UsageJournal
from agent.adapters.usage import MeteredProvider
from agent.adapters.verification import VerifiedTools, workspace_fingerprint
from agent.cli import main
from agent.core.engine import Agent
from agent.core.types import ConversationState, Message, ModelResponse, ModelUsage, ToolCall
from agent.files import Workspace
from agent.server import Server


class ProductionFixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        env = patch.dict(os.environ, {'AGENT_STATE_DIR': str(self.root / 'state'), 'AGENT_CONFIG_DIR': str(self.root / 'config')})
        env.start()
        self.addCleanup(env.stop)
        self.workspace = Workspace(self.project, True)

    def invoke(self, args):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['coding-agent', *args]), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            main()
        return out.getvalue(), err.getvalue()

    def file(self, name, content='value = 1\n'):
        path = self.project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path


class PolicyTests(ProductionFixture):
    def test_shared_gitignore_and_agentignore_cases(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/ignore-policy.json').read_text())
        for name, text in fixture['rules'].items():
            self.file(name, text)
        policy = FilePolicy(self.project)
        for case in fixture['cases']:
            self.file(case['path'])
            with self.subTest(path=case['path']):
                self.assertEqual(policy.denied(case['path']), case['denied'])
                self.assertEqual(policy.ignored(case['path']), case['ignored'])

    def test_explicit_ignored_file_is_readable_but_denied_file_cannot_be_read_attached_or_written(self):
        from agent.adapters.attachments import Attachments
        self.file('.gitignore', '*.log\n')
        self.file('.agentignore', 'private/\n')
        self.file('private/key.txt', 'sensitive')
        self.file('trace.log', 'searchable')
        self.assertEqual(self.workspace.execute('read_file', {'path': 'trace.log'}), 'searchable')
        for action in [lambda: self.workspace.execute('read_file', {'path': 'private/key.txt'}),
                       lambda: self.workspace.execute('write_file', {'path': 'private/key.txt', 'content': 'bad'}),
                       lambda: Attachments(self.workspace).add('private/key.txt')]:
            with self.assertRaises(ValueError):
                action()
        listing = self.workspace.execute('list_files', {'path': '.'})
        self.assertNotIn('trace.log', listing)
        self.assertNotIn('private', listing)
        result = workspace_tools(self.workspace).execute(ToolCall('id', 'search_text', json.dumps({'path': '.', 'query': 'sensitive'})))
        self.assertEqual(json.loads(result.content)['matches'], [])

    def test_policy_reload_and_policy_symlink_fail_closed(self):
        target = self.file('a.py')
        self.file('.agentignore', 'a.py\n')
        with self.assertRaises(ValueError):
            self.workspace.path('a.py')
        self.file('.agentignore', '')
        self.assertEqual(self.workspace.path('a.py'), target)
        if sys.platform != 'win32':
            (self.project / '.agentignore').unlink()
            (self.project / '.agentignore').symlink_to(target)
            with self.assertRaises(ValueError):
                self.workspace.path('a.py')

    def test_unreviewed_writer_is_atomic_on_commit_failure(self):
        path = self.file('a.py', 'original')
        with patch('agent.adapters.review.os.replace', side_effect=OSError('disk failure')), self.assertRaises(OSError):
            self.workspace.execute('write_file', {'path': 'a.py', 'content': 'replacement'})
        self.assertEqual(path.read_text(), 'original')
        self.assertEqual(list(self.project.glob('.agent-review-*')), [])


class VerificationTests(ProductionFixture):
    def test_truncated_output_counts_usage_without_executing_partial_tools(self):
        model = Mock()
        model.generate.return_value = ModelResponse(Message('assistant', tool_calls=(
            ToolCall('write', 'write_file', json.dumps({'path': 'a.py', 'content': 'partial'})),)), ModelUsage(10, 4096), True)
        journal = UsageJournal()
        provider = MeteredProvider(model, journal, 'session', provider_name='demo', model='demo')
        state = ConversationState()
        with self.assertRaisesRegex(RuntimeError, 'output limit'):
            Agent(provider, workspace_tools(self.workspace)).run('task', state)
        self.assertFalse((self.project / 'a.py').exists())
        self.assertEqual(state.messages, [])
        self.assertEqual(journal.query({'period': 'all'})['totals']['tokens']['output_tokens']['reported_sum'], 4096)

    def tracker(self):
        runner = LocalCommandRunner(self.project, [sys.executable, '-c', 'print("ok")'])
        tools = workspace_tools(self.workspace, test_runner=runner)
        return VerifiedTools(tools, lambda: workspace_fingerprint(self.workspace), {'test': runner.command})

    def test_actual_pass_is_stale_after_external_edit_and_configuration_change(self):
        self.file('a.py')
        tracker = self.tracker()
        self.assertTrue(tracker.execute(ToolCall('1', 'run_tests', '{}')).ok)
        self.assertEqual(tracker.evidence()[0].status, 'passed')
        self.file('a.py', 'external change')
        self.assertEqual(tracker.evidence()[0].status, 'stale')
        tracker.execute(ToolCall('2', 'run_tests', '{}'))
        changed = VerifiedTools(tracker.tools, tracker.snapshot, {'test': ['different']}, tracker.records)
        self.assertEqual(changed.evidence()[0].status, 'stale')

    def test_successful_check_that_changes_inputs_is_stale(self):
        self.file('a.py')
        runner = LocalCommandRunner(self.project, [sys.executable, '-c', 'from pathlib import Path; Path("a.py").write_text("changed")'])
        tracker = VerifiedTools(workspace_tools(self.workspace, test_runner=runner), lambda: workspace_fingerprint(self.workspace))
        self.assertTrue(tracker.execute(ToolCall('1', 'run_tests', '{}')).ok)
        self.assertEqual(tracker.evidence()[0].status, 'stale')

    def test_uncertain_snapshot_never_passes_and_missing_check_is_not_run(self):
        tracker = self.tracker()
        tracker.snapshot = lambda: None
        tracker.execute(ToolCall('1', 'run_tests', '{}'))
        self.assertEqual(tracker.evidence()[0].status, 'unknown')
        self.assertEqual(tracker.evidence()[1].status, 'not_run')

    def test_model_claim_cannot_create_verification_and_failed_turn_exposes_staleness(self):
        tracker = self.tracker()
        model = Mock()
        model.generate.return_value = ModelResponse(Message('assistant', 'All tests pass'))
        result = Agent(model, tracker, verification=tracker).run('task')
        self.assertEqual(result.verification[0].status, 'not_run')
        tracker.execute(ToolCall('1', 'run_tests', '{}'))
        tracker.execute(ToolCall('2', 'write_file', json.dumps({'path': 'a.py', 'content': 'new'})))
        model.generate.side_effect = RuntimeError('failed')
        events = Mock()
        with self.assertRaises(RuntimeError):
            Agent(model, tracker, events, verification=tracker).run('task')
        evidence = [call.args[1] for call in events.emit.call_args_list if call.args[0] == 'verification'][0]
        self.assertEqual(evidence['results'][0]['status'], 'stale')


class RecoveryTests(ProductionFixture):
    def setUp(self):
        super().setUp()
        self.now = 1800000000
        self.store = RecoveryStore(clock=lambda: self.now)
        self.identifier = str(uuid4())
        self.state = ConversationState([Message('user', 'source excerpt'), Message('assistant', 'plan')], 'plan', 'accepted')
        self.connection = {'provider': 'ollama', 'profile': 'local', 'model': 'model'}

    def save(self):
        self.store.save(self.identifier, self.workspace, self.state, self.connection)

    def test_recovery_is_opt_in_completed_and_resumes_without_plan_acceptance(self):
        self.assertFalse(self.store.directory.exists())
        self.file('a.py')
        self.save()
        state, connection, changed = self.store.resume(self.identifier, self.workspace)
        self.assertIsNone(state.accepted_plan)
        self.assertEqual(state.messages, self.state.messages)
        self.assertEqual(connection, self.connection)
        self.assertFalse(changed)
        self.file('a.py', 'changed')
        self.assertTrue(self.store.resume(self.identifier, self.workspace)[2])
        self.assertNotIn('state', self.store.list(self.workspace)[0])

    def test_expiry_corrupt_snapshot_unknown_schema_and_wrong_workspace(self):
        self.save()
        with tempfile.TemporaryDirectory() as other, self.assertRaises(ValueError):
            self.store.resume(self.identifier, Workspace(Path(other)))
        path = self.store.path(self.identifier)
        value = json.loads(path.read_text())
        value['version'] = 999
        path.write_text(json.dumps(value))
        self.assertEqual(self.store.list(self.workspace), [])
        self.assertTrue(path.exists())
        self.save()
        self.now += 30 * 86400
        with self.assertRaises(ValueError):
            self.store.resume(self.identifier, self.workspace)
        self.assertFalse(path.exists())

    def test_interrupted_save_preserves_last_snapshot_and_partial_turns_are_rejected(self):
        self.save()
        path = self.store.path(self.identifier)
        before = path.read_bytes()
        with patch('agent.adapters.recovery.os.replace', side_effect=OSError('disk full')), self.assertRaises(OSError):
            self.save()
        self.assertEqual(path.read_bytes(), before)
        value = json.loads(path.read_text())
        value['state']['messages'].append(asdict(Message('user', 'unfinished')))
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            self.store.resume(self.identifier, self.workspace)

    def test_cli_saves_only_when_enabled_and_read_only_server_recovery_needs_no_provider(self):
        self.store = RecoveryStore()
        args = ['task', '--workspace', str(self.project), '--provider', 'demo']
        self.invoke(args)
        self.assertFalse(self.store.directory.exists())
        self.invoke(args + ['--persist-session'])
        items = self.store.list(self.workspace)
        self.assertEqual(len(items), 1)
        output = io.StringIO()
        server = Server(output)
        server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'session/resumeSaved',
                       'params': {'workspace': str(self.project), 'sessionId': items[0]['id']}})
        result = json.loads(output.getvalue())['result']
        self.assertIsNone(result['checkpoint']['accepted_plan'])
        self.assertIsNone(server.config)


class OperationalTests(ProductionFixture):
    def test_noninteractive_write_requires_explicit_review_opt_out(self):
        with patch('sys.stdin.isatty', return_value=False), self.assertRaises(SystemExit) as error:
            self.invoke(['task', '--provider', 'demo', '--workspace', str(self.project), '--allow-write'])
        self.assertEqual(error.exception.code, 2)
        self.invoke(['task', '--provider', 'demo', '--workspace', str(self.project), '--allow-write', '--no-review-edits'])

    def test_diagnostics_are_allowlisted_and_export_never_overwrites(self):
        with patch('agent.adapters.diagnostics.OSVault.backend', side_effect=ValueError('secret-key')), \
                patch('agent.adapters.diagnostics.ProfileStore.load', side_effect=ValueError('source-code')):
            report = diagnose({'test': [sys.executable, '--sensitive-argument']})
        text = json.dumps(report)
        for private in ('secret-key', 'source-code', '--sensitive-argument'):
            self.assertNotIn(private, text)
        self.assertTrue(report['checks']['test'])
        destination = self.root / 'diagnostics.json'
        export_report(destination, report)
        with self.assertRaises(FileExistsError):
            export_report(destination, report)
        out, _ = self.invoke(['doctor'])
        self.assertFalse(json.loads(out)['model_requests_made'])

    def test_finalized_usage_releases_memory_and_failed_buffer_is_bounded(self):
        journal = UsageJournal(max_memory=3)
        for _ in range(10):
            request = journal.start('s', 'r', 'demo', 'ad-hoc', 'demo', 'cli', 'agent')
            journal.finish(request, ModelUsage(1, 1))
        self.assertEqual(journal.memory, {})
        self.assertEqual(journal.query({'period': 'all'})['totals']['calls'], 10)
        with patch.object(journal, 'connect', side_effect=sqlite3.OperationalError('locked')):
            for _ in range(10):
                request = journal.start('s', 'r', 'demo', 'ad-hoc', 'demo', 'cli', 'agent')
                journal.finish(request, ModelUsage(1, 1))
        self.assertEqual(len(journal.memory), 3)
        self.assertEqual(journal.dropped, 7)
        self.assertTrue(journal.query({'period': 'all'})['storage_incomplete'])

    def test_usage_pruning_is_explicit_and_preserves_pending_attempts(self):
        journal = UsageJournal(clock=lambda: 1600000000)
        request = journal.start('s', 'r', 'demo', 'ad-hoc', 'demo', 'cli', 'agent')
        journal.finish(request, ModelUsage(2, 1))
        journal.start('s', 'r', 'demo', 'ad-hoc', 'demo', 'cli', 'agent')
        self.assertEqual(journal.prune('2026-01-01'), 1)
        self.assertEqual(journal.query({'period': 'all'})['totals']['unfinished'], 1)
