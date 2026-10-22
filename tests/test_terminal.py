import builtins
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.adapters.tools import workspace_tools
from agent.core.engine import Agent
from agent.core.types import Message, ModelResponse
from agent.files import Workspace
from agent.terminal import InteractiveShell, command_candidates, create_prompt_session


class TerminalTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = Workspace(self.root, allow_write=True)
        self.output = []
        self.calls = []
        owner = self

        class Model:
            def generate(self, messages, tools):
                owner.calls.append(tuple(messages))
                return ModelResponse(Message("assistant", "Proposed plan"))
        self.model = Model()
        self.shell = InteractiveShell(self.workspace, self.factory, output=self.output.append)

    def factory(self, mode, rules):
        return Agent(self.model, workspace_tools(self.workspace), mode=mode, project_instructions=rules)

    def test_commands_are_local_and_implement_waits(self):
        for command in ["/help", "/instructions", "/plan", "/implement", "/plna", "", "/exit"]:
            self.shell.handle(command)
        self.assertEqual(self.calls, [])
        self.assertTrue(any("Try:" in message for message in self.output))

    def test_inline_plan_and_accepted_plan_survive_follow_up(self):
        self.shell.handle("/plan Change a function")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.shell.state.latest_plan, "Proposed plan")
        self.shell.handle("/implement")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.shell.state.accepted_plan, "Proposed plan")
        self.assertTrue(self.shell.writable)
        self.shell.handle("Do it")
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(any("Proposed plan" in m.content and m.role == "system" for m in self.calls[-1]))

    def test_new_resets_and_reloads_rules(self):
        self.shell.handle("Task")
        self.shell.handle("/implement")
        (self.root / "AGENTS.md").write_text("New rules")
        self.shell.handle("/instructions")
        self.assertNotIn("New rules", self.output[-1])
        self.shell.handle("/new")
        self.assertEqual(self.shell.mode, "plan")
        self.assertEqual(self.shell.state.messages, [])
        self.assertIsNone(self.shell.state.accepted_plan)
        self.assertIsNone(self.shell.state.latest_plan)
        self.assertEqual(self.shell.instructions.content, "New rules")
        self.shell.handle("/instructions")
        self.assertIn("New rules", self.output[-1])

    def test_invalid_reload_keeps_session(self):
        self.shell.handle("Task")
        old = list(self.shell.state.messages)
        (self.root / "AGENTS.md").write_bytes(b'\xff')
        self.shell.handle("/new")
        self.assertEqual(self.shell.state.messages, old)
        self.assertIn("Session unchanged", self.output[-1])

    def test_write_permission_warning_and_prompt(self):
        shell = InteractiveShell(Workspace(self.root), self.factory, output=self.output.append)
        self.assertIn("read-only", shell.prompt)
        shell.handle("/implement")
        self.assertFalse(shell.writable)
        self.assertIn("--allow-write", self.output[-1])

    def test_command_completion(self):
        self.assertEqual(command_candidates("/pl"), ("/plan",))
        self.assertEqual(command_candidates("/i"), ("/implement", "/instructions"))
        self.assertTrue({'/status', '/attach', '/attachments', '/detach'}.issubset(command_candidates('/')))
        self.assertEqual(command_candidates("/plan task"), ())
        self.assertEqual(command_candidates("ordinary task"), ())

    def test_bad_command_arguments_do_not_execute(self):
        self.shell.handle("/implement do it")
        self.assertEqual(self.shell.mode, "plan")
        self.assertEqual(self.calls, [])
        self.assertIn("does not accept", self.output[-1])

    def test_prompt_interrupt_eof_and_execution_interrupt(self):
        class Session:
            def __init__(self):
                self.events = iter([KeyboardInterrupt(), "Task", EOFError()])

            def prompt(self, text):
                value = next(self.events)
                if isinstance(value, BaseException):
                    raise value
                return value
        self.shell.run(Session())
        self.assertEqual(len(self.calls), 1)
        original = list(self.shell.state.messages)
        with patch.object(self.model, "generate", side_effect=KeyboardInterrupt):
            self.shell.handle("Interrupted task")
        self.assertEqual(self.shell.state.messages, original)
        self.assertTrue(any('No automatic retry' in line for line in self.output))
        self.shell.handle("Next task")
        self.assertEqual(len(self.calls), 2)

    def test_failure_keeps_terminal_usable(self):
        with patch.object(self.model, "generate", side_effect=RuntimeError("network")):
            self.shell.handle("Failed task")
        self.assertEqual(self.shell.state.messages, [])
        self.assertTrue(any('Run failed' in line for line in self.output))
        self.shell.handle("Working task")
        self.assertEqual(len(self.calls), 1)

    def test_optional_dependency_error(self):
        original = builtins.__import__
        def importer(name, *args, **kwargs):
            if name.startswith("prompt_toolkit"):
                raise ImportError("not installed")
            return original(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=importer):
            with self.assertRaisesRegex(RuntimeError, 'terminal'):
                create_prompt_session()


class RealPromptTests(unittest.TestCase):
    def test_tab_completes_actual_prompt(self):
        try:
            from prompt_toolkit.input.defaults import create_pipe_input
            from prompt_toolkit.output import DummyOutput
        except ImportError:
            self.skipTest("Install .[terminal] for real prompt completion test")
        with create_pipe_input() as pipe:
            session = create_prompt_session(input=pipe, output=DummyOutput())
            pipe.send_text('/pl\t\r')
            self.assertEqual(session.prompt('> '), '/plan')


class VerificationPromptTests(unittest.TestCase):
    def test_each_verification_capability_is_visible_and_mode_gated(self):
        with tempfile.TemporaryDirectory() as directory:
            shell = InteractiveShell(Workspace(Path(directory), allow_write=True), lambda *args: None,
                                     execution_enabled=True, lint_enabled=True, lint_fix_enabled=True,
                                     output=lambda text: None)
            self.assertIn('tests:off', shell.prompt)
            self.assertIn('lint:off', shell.prompt)
            self.assertIn('fixes:off', shell.prompt)
            shell.handle('/implement')
            self.assertIn('tests:on', shell.prompt)
            self.assertIn('lint:on', shell.prompt)
            self.assertIn('fixes:on', shell.prompt)
