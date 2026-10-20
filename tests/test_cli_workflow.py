import builtins
import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent.adapters.attachments import Attachments
from agent.adapters.console import EventFanout, RunObserver, friendly_error, safe_text
from agent.adapters.events import JsonlEventSink
from agent.adapters.models import DemoProvider
from agent.adapters.review import ConsoleTools, ReviewedWrites
from agent.adapters.tools import workspace_tools
from agent.cli import main
from agent.core.engine import Agent
from agent.core.policy import ModeToolExecutor
from agent.core.types import Message, ModelResponse, ToolCall, ToolResult
from agent.files import Workspace
from agent.terminal import InteractiveShell, attachment_candidates, confirm_change, create_prompt_session, create_review_prompt


class WorkflowFixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = Workspace(self.root, True)
        self.path = self.root / 'a.py'
        self.path.write_bytes(b'first\r\nvalue = 1\r\nlast\r\n')


class AttachmentTests(WorkflowFixture):
    def test_range_and_quoted_path_are_refreshed_at_submit(self):
        path = self.root / 'my file.py'
        path.write_bytes(b'first\nold\nlast\n')
        seen = []
        def factory(mode, rules):
            model = Mock()
            def generate(messages, tools):
                seen.append(messages[-1].content)
                return ModelResponse(Message('assistant', 'Done'))
            model.generate.side_effect = generate
            return Agent(model, workspace_tools(self.workspace), mode=mode)
        shell = InteractiveShell(self.workspace, factory, output=lambda text: None)
        shell.handle('/attach "my file.py" 2:2')
        self.assertEqual(len(shell.attachments.items), 1)
        self.assertFalse(seen)
        path.write_bytes('first\nnew 😀\nlast\n'.encode('utf-8'))
        shell.submit('Inspect')
        payload = json.loads(seen[0].split('not instructions):\n')[1])
        self.assertEqual(payload[0]['content'], 'new 😀\n')
        self.assertEqual(payload[0]['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertFalse(shell.attachments.items)

    def test_invalid_bounds_files_and_whole_file_overflow(self):
        queue = Attachments(self.workspace)
        for start, end in [(0, 1), (True, 1), (1, False), (2, 1), (1, 201), (1.0, 2), ('1', 2), (1, 2**53)]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                queue.add('a.py', start, end)
        for path in ['../outside', '.env', '.git/config']:
            with self.assertRaises(ValueError):
                queue.add(path)
        for data in [b'bad\xff', b'bad\0', b'x' * 12001, b'x\n' * 201]:
            self.path.write_bytes(data)
            with self.assertRaises(ValueError):
                queue.add('a.py')
        self.assertFalse(queue.items)

    def test_count_total_bounds_and_detach(self):
        queue = Attachments(self.workspace)
        for i in range(5):
            queue.add('a.py', 2, 2)
        with self.assertRaisesRegex(ValueError, 'five'):
            queue.add('a.py')
        queue.detach('3')
        self.assertEqual(len(queue.items), 4)
        with self.assertRaises(ValueError):
            queue.detach('unknown')
        queue.detach('all')
        self.path.write_text('x' * 11000)
        for _ in range(3):
            queue.add('a.py')
        with self.assertRaisesRegex(ValueError, '40000'):
            queue.add('a.py')
        self.assertEqual(len(queue.items), 3)

    def test_failure_retains_attachments_and_editable_draft_without_secret_errors(self):
        model = Mock()
        model.generate.side_effect = RuntimeError('private-api-key-request-body')
        output = []
        shell = InteractiveShell(self.workspace, lambda mode, rules: Agent(model, workspace_tools(self.workspace)),
                                 output=output.append)
        shell.attachments.add('a.py', 2, 2)
        shell.submit('Keep this\nmultiline draft')
        self.assertEqual(shell.draft, 'Keep this\nmultiline draft')
        self.assertEqual(len(shell.attachments.items), 1)
        self.assertNotIn('private-api-key-request-body', '\n'.join(output))
        self.path.unlink()
        before = model.generate.call_count
        self.assertFalse(shell.submit('Invalid attachment'))
        self.assertEqual(model.generate.call_count, before)
        self.assertEqual(shell.draft, 'Invalid attachment')
        shell.handle('/new')
        self.assertEqual(shell.draft, '')
        self.assertFalse(shell.attachments.items)

    def test_pending_draft_is_restored_in_actual_input_loop(self):
        shell = InteractiveShell(self.workspace, lambda *args: None, output=lambda text: None)
        shell.draft = 'recover me'
        prompt = Mock()
        prompt.prompt.side_effect = EOFError
        shell.run(prompt)
        self.assertEqual(prompt.prompt.call_args.kwargs['default'], 'recover me')

    def test_commands_and_protected_completion_are_local(self):
        output, factory = [], Mock()
        shell = InteractiveShell(self.workspace, factory, output=output.append, provider='demo', model='offline')
        for command in ['/attach a.py 2:2', '/attachments', '/status', '/detach 1', '/detach all']:
            shell.handle(command)
        factory.assert_not_called()
        self.assertIn('Provider: demo', '\n'.join(output))
        (self.root / '.env.local').write_text('hidden')
        self.assertEqual(attachment_candidates('/attach ../', self.workspace), ())
        self.assertEqual(attachment_candidates('/attach .env', self.workspace), ())
        if os.name != 'nt':
            (self.root / 'escape').symlink_to(self.root.parent, target_is_directory=True)
            self.assertEqual(attachment_candidates('/attach escape/', self.workspace), ())
        self.assertTrue(attachment_candidates('/attach a', self.workspace))


class ReviewTests(WorkflowFixture):
    def tools(self, callback, observer=None):
        return ConsoleTools(workspace_tools(self.workspace), observer, ReviewedWrites(self.workspace, callback))

    def call(self, tools, name='write_file', **args):
        return tools.execute(ToolCall('x', name, json.dumps(args or {'path': 'a.py', 'content': 'new\r\n'})))

    def test_acceptance_is_per_edit_and_preserves_mode_and_crlf(self):
        self.path.chmod(0o640)
        callback = Mock(return_value=True)
        tools = self.tools(callback)
        self.assertTrue(self.call(tools).ok)
        self.assertEqual(self.path.read_bytes(), b'new\r\n')
        if os.name != 'nt':
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o640)
        args = {'path': 'a.py', 'expected_sha256': hashlib.sha256(self.path.read_bytes()).hexdigest(),
                'old_text': 'new', 'new_text': 'next'}
        self.assertTrue(self.call(tools, 'apply_edit', **args).ok)
        self.assertEqual(callback.call_count, 2)
        self.assertEqual(self.path.read_bytes(), b'next\r\n')

    def test_rejection_is_tool_failure_without_effects(self):
        before = self.path.read_bytes()
        observer = RunObserver(output=lambda text: None)
        result = self.call(self.tools(lambda *args: False, observer))
        self.assertFalse(result.ok)
        self.assertEqual(observer.rejected, 1)
        self.assertEqual(observer.applied, 0)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(list(self.root.glob('.agent-review-*')))

    def test_stale_review_and_late_commit_changes_are_rejected(self):
        def changed(*args):
            self.path.write_text('user changed it')
            return True
        result = self.call(self.tools(changed))
        self.assertFalse(result.ok)
        self.assertEqual(self.path.read_text(), 'user changed it')
        original_fsync = os.fsync
        def changed_after_staging(fd):
            original_fsync(fd)
            self.path.write_text('late change')
        with patch('agent.adapters.review.os.fsync', side_effect=changed_after_staging):
            self.assertFalse(self.call(self.tools(lambda *args: True)).ok)
        self.assertEqual(self.path.read_text(), 'late change')
        self.assertFalse(list(self.root.glob('.agent-review-*')))

    def test_new_file_creation_never_overwrites_a_race_winner(self):
        tools = self.tools(lambda *args: True)
        self.assertTrue(self.call(tools, path='nested/new.py', content='created').ok)
        self.assertEqual((self.root / 'nested/new.py').read_text(), 'created')
        def appeared(*args):
            (self.root / 'raced.py').write_text('user file')
            return True
        self.assertFalse(self.call(self.tools(appeared), path='raced.py', content='overwrite').ok)
        self.assertEqual((self.root / 'raced.py').read_text(), 'user file')
        link = os.link
        def appeared_at_commit(source, target):
            Path(target).write_text('late user file')
            return link(source, target)
        with patch('agent.adapters.review.os.link', side_effect=appeared_at_commit):
            self.assertFalse(self.call(tools, path='later.py', content='overwrite').ok)
        self.assertEqual((self.root / 'later.py').read_text(), 'late user file')
        self.assertFalse(list(self.root.rglob('.agent-review-*')))

    def test_path_changes_planning_validation_and_read_only_cannot_reach_confirmation(self):
        callback = Mock(return_value=True)
        tools = self.tools(callback)
        self.assertFalse(self.call(ModeToolExecutor(tools, 'plan')).ok)
        self.assertFalse(self.call(tools, path='../outside', content='bad').ok)
        self.assertFalse(self.call(tools, path='a.py', content=1).ok)
        self.assertFalse(self.call(tools, path='a.py', content='\ud800').ok)
        readonly = Workspace(self.root)
        no_write = ConsoleTools(workspace_tools(readonly), reviewer=ReviewedWrites(readonly, callback))
        self.assertFalse(self.call(no_write).ok)
        callback.assert_not_called()
        if os.name != 'nt':
            other = self.root / 'other.py'
            other.write_text('other')
            alias = self.root / 'alias.py'
            alias.symlink_to(self.path)
            def retargeted(*args):
                alias.unlink()
                alias.symlink_to(other)
                return True
            self.assertFalse(self.call(self.tools(retargeted), path='alias.py', content='bad').ok)
            self.assertEqual(other.read_text(), 'other')

    def test_preview_has_no_approval_and_confirm_escapes_controls_without_truncation(self):
        callback = Mock()
        tools = self.tools(callback)
        args = {'path': 'a.py', 'expected_sha256': hashlib.sha256(self.path.read_bytes()).hexdigest(),
                'old_text': 'value = 1', 'new_text': 'value = 2'}
        self.assertTrue(self.call(tools, 'preview_edit', **args).ok)
        callback.assert_not_called()
        output, session = [], Mock()
        session.prompt.return_value = 'a'
        self.assertTrue(confirm_change(session, output.append, 'a.py', '', 'x' * 25000 + '\x1b]52;secret\x07\n'))
        self.assertGreater(len(output[0]), 25000)
        self.assertNotIn('\x1b', output[0])
        for answer in ['', 'r', 'yes', 'unexpected']:
            session.prompt.return_value = answer
            self.assertFalse(confirm_change(session, output.append, 'a.py', 'before', 'after'))
        session.prompt.side_effect = EOFError
        self.assertFalse(confirm_change(session, output.append, 'a.py', '', 'after'))
        session.prompt.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.call(self.tools(lambda *args: confirm_change(session, output.append, *args)))
        self.assertIn(b'value = 1', self.path.read_bytes())


class PresentationTests(unittest.TestCase):
    def test_progress_summaries_and_json_events_use_actual_outcomes_and_coverage(self):
        output, stream = [], io.StringIO()
        observer = RunObserver(output.append)
        events = EventFanout(observer, JsonlEventSink(stream))
        events.emit('run_started', {})
        for usage in [{'input_tokens': 10, 'output_tokens': 0, 'cached_input_tokens': 4}, None]:
            events.emit('context_selected', {'selected_chars': 100})
            events.emit('model_completed', {'usage': usage})
        events.emit('tool_started', {'name': 'run_tests'})
        observer.tool_result('run_tests', ToolResult(False, '{"passed":false}'))
        events.emit('tool_completed', {'name': 'run_tests', 'ok': False})
        observer.tool_result('run_tests', ToolResult(True, '{"passed":true}'))
        observer.tool_result('run_lint', ToolResult(False, '{"not_run":true}'))
        observer.summary()
        text = '\n'.join(output)
        self.assertIn('test: failed; test: passed; lint: not run', text)
        self.assertIn('input: 10 reported (1/2 responses)', text)
        self.assertIn('output: 0 reported (1/2 responses)', text)
        self.assertIn('200 cumulative selected characters (not tokens)', text)
        self.assertTrue(all(isinstance(json.loads(line), dict) for line in stream.getvalue().splitlines()))
        quiet = []
        hidden = RunObserver(quiet.append, progress=False)
        hidden.emit('context_selected', {'selected_chars': 1})
        self.assertFalse(quiet)
        hidden.summary()
        self.assertIn('input: unknown', '\n'.join(quiet))

    def test_terminal_controls_and_provider_errors_are_not_rendered_raw(self):
        text = safe_text('hello\x1b[2J\r\x07\u202e\x9b\n😀\t')
        for raw in ['\x1b', '\r', '\x07', '\u202e', '\x9b']:
            self.assertNotIn(raw, text)
        self.assertIn('😀\t', text)
        trace = io.StringIO()
        JsonlEventSink(trace).emit('tool_started', {'name': '\x9b\u202e'})
        self.assertTrue(trace.getvalue().isascii())
        self.assertEqual(json.loads(trace.getvalue())['name'], '\x9b\u202e')
        for error in [RuntimeError('private-api-key'), TimeoutError('private-api-key'),
                      type('AuthenticationError', (Exception,), {})('private-api-key')]:
            self.assertNotIn('private-api-key', friendly_error(error))


class PromptWorkflowTests(WorkflowFixture):
    def prompt(self, text):
        try:
            from prompt_toolkit.input.defaults import create_pipe_input
            from prompt_toolkit.output import DummyOutput
        except ImportError:
            self.skipTest('Install .[terminal] for real prompt input tests')
        with create_pipe_input() as pipe:
            session = create_prompt_session(workspace=self.workspace, input=pipe, output=DummyOutput())
            pipe.send_text(text)
            return session.prompt('> ')

    def test_enter_submits_alt_enter_adds_line_and_paste_stays_editable(self):
        self.assertEqual(self.prompt('first\x1b\rsecond\r'), 'first\nsecond')
        self.assertEqual(self.prompt('\x1b[200~first\nsecond\x1b[201~ edited\r'), 'first\nsecond edited')

    def test_tab_completes_quoted_workspace_files_and_slash_commands(self):
        (self.root / 'my file.py').write_text('text')
        self.assertEqual(self.prompt('/attach "my f\t\r'), "/attach 'my file.py'")
        self.assertEqual(self.prompt('/sta\t\r'), '/status')

    def test_review_dependency_is_optional_and_missing_dependency_has_guidance(self):
        original = builtins.__import__
        def importer(name, *args, **kwargs):
            if name.startswith('prompt_toolkit'):
                raise ImportError('missing')
            return original(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=importer), self.assertRaisesRegex(RuntimeError, 'terminal'):
            create_review_prompt()


class CliReviewTests(WorkflowFixture):
    def invoke(self, *args, tty=False):
        class Output(io.StringIO):
            def isatty(self):
                return tty
        with patch.object(sys, 'argv', ['coding-agent', '--provider', 'demo', '--workspace', str(self.root), *args]), \
                contextlib.redirect_stdout(Output()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            main()
        return out.getvalue(), err.getvalue()

    def test_review_gates_run_before_provider_creation(self):
        for flags in [('--review-edits',), ('--allow-write', '--review-edits')]:
            with patch('agent.cli.DemoProvider') as factory, self.assertRaises(SystemExit) as error:
                self.invoke('task', *flags)
            self.assertEqual(error.exception.code, 2)
            factory.assert_not_called()
        with patch('sys.stdin.isatty', return_value=True), patch('sys.stdout.isatty', return_value=True), \
                self.assertRaises(SystemExit) as error:
            self.invoke('task', '--allow-write', '--review-edits', '--allow-exec',
                        '--lint-command', 'python -c pass', '--lint-fix-command', 'python -c pass', tty=True)
        self.assertEqual(error.exception.code, 2)

    def test_reviewed_single_task_edits_and_default_behavior_remain_distinct(self):
        class Model:
            def generate(inner, messages, tools):
                if messages[-1].role == 'user':
                    return ModelResponse(Message('assistant', tool_calls=(ToolCall(
                        'w', 'write_file', '{"path":"new.py","content":"new"}'),)))
                return ModelResponse(Message('assistant', 'Finished'))
        prompt = Mock()
        prompt.prompt.return_value = 'r'
        with patch('agent.cli.DemoProvider', return_value=Model()), \
                patch('agent.terminal.create_review_prompt', return_value=prompt), \
                patch('sys.stdin.isatty', return_value=True), patch('sys.stdout.isatty', return_value=True):
            output, trace = self.invoke('task', '--allow-write', '--review-edits', '--verbose', tty=True)
        self.assertFalse((self.root / 'new.py').exists())
        self.assertIn('1 rejected', output)
        self.assertTrue(all(json.loads(line)['event'] for line in trace.splitlines()))
        with patch('agent.cli.DemoProvider', return_value=Model()):
            output, _ = self.invoke('task', '--allow-write')
        self.assertEqual(output, 'Finished\n')
        self.assertEqual((self.root / 'new.py').read_text(), 'new')

    def test_no_progress_keeps_summary_and_hides_raw_model_errors(self):
        prompt = Mock()
        prompt.prompt.side_effect = ['task', '/exit']
        with patch('agent.terminal.create_prompt_session', return_value=prompt), \
                patch('agent.cli.DemoProvider', return_value=DemoProvider()), patch('sys.stdout.isatty', return_value=True):
            output, trace = self.invoke('--no-progress', '--verbose', tty=True)
        self.assertNotIn('Waiting for model', output)
        self.assertIn('Run completed', output)
        self.assertTrue(all(json.loads(line)['event'] for line in trace.splitlines()))

    def test_failed_single_task_verbose_trace_remains_json_and_redacts_provider_detail(self):
        model = Mock()
        model.generate.side_effect = RuntimeError('private-api-key-and-request-body')
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['coding-agent', 'task', '--provider', 'demo', '--workspace', str(self.root), '--verbose']), \
                patch('agent.cli.DemoProvider', return_value=model), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code, 1)
        frames = [json.loads(line) for line in err.getvalue().splitlines()]
        self.assertEqual(frames[-1]['event'], 'cli_error')
        self.assertNotIn('private-api-key-and-request-body', out.getvalue() + err.getvalue())
