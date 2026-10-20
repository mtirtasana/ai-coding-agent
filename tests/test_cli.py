import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.cli import main


class CliTests(unittest.TestCase):
    def invoke(self, *arguments):
        output, error = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', ['coding-agent', *arguments]), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            main()
        return output.getvalue(), error.getvalue()

    def test_single_task_defaults_to_implementation_and_loads_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'AGENTS.md').write_text('Use explicit types')
            from agent.adapters.models import DemoProvider
            provider = DemoProvider()
            with patch.object(provider, 'generate', wraps=provider.generate) as generate, \
                    patch('agent.cli.DemoProvider', return_value=provider):
                output, error = self.invoke('Inspect', '--provider', 'demo', '--workspace', directory, '--verbose')
            messages = generate.call_args_list[0].args[0]
            self.assertTrue(any('Use explicit types' in message.content for message in messages))
            self.assertIn('"mode": "implement"', error)
            self.assertIn('Offline-Demo', output)

    def test_interactive_defaults_to_plan_and_exit_uses_no_model(self):
        class Prompt:
            def prompt(self, text):
                if 'plan' not in text:
                    raise AssertionError('Expected planning mode')
                return '/exit'
        with tempfile.TemporaryDirectory() as directory, \
                patch('agent.terminal.create_prompt_session', return_value=Prompt()), \
                patch('agent.cli.DemoProvider') as model:
            self.invoke('--provider', 'demo', '--workspace', directory)
            model.return_value.generate.assert_not_called()

    def test_explicit_plan_mode_blocks_write_schema(self):
        from agent.adapters.models import DemoProvider
        provider = DemoProvider()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(provider, 'generate', wraps=provider.generate) as generate, \
                patch('agent.cli.DemoProvider', return_value=provider):
            self.invoke('Inspect', '--provider', 'demo', '--workspace', directory,
                        '--mode', 'plan', '--allow-write')
            self.assertNotIn('write_file', {spec.name for spec in generate.call_args_list[0].args[1]})

    def test_invalid_rules_fail_before_model_creation(self):
        with tempfile.TemporaryDirectory() as directory, patch('agent.cli.DemoProvider') as model:
            (Path(directory) / 'AGENTS.md').write_bytes(b'\xff')
            with self.assertRaises(SystemExit) as raised:
                self.invoke('Inspect', '--provider', 'demo', '--workspace', directory)
            self.assertEqual(raised.exception.code, 1)
            model.assert_not_called()

    def test_execution_requires_both_configuration_and_permission(self):
        for flags in [('--allow-exec',), ('--test-command', 'python -m unittest')]:
            with self.assertRaises(SystemExit) as result:
                self.invoke('Inspect', '--provider', 'demo', *flags)
            self.assertEqual(result.exception.code, 2)

    def test_compatible_cli_selects_endpoint_and_key_environment(self):
        from agent.adapters.models import DemoProvider
        provider = DemoProvider()
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict('os.environ', {'TEST_COMPATIBLE_KEY': 'test-only'}), \
                patch('agent.cli.OpenAICompatibleProvider', return_value=provider) as factory:
            self.invoke('Inspect', '--provider', 'openai-compatible', '--model', 'local-test',
                        '--base-url', 'http://localhost:1234/v1', '--api-key-env', 'TEST_COMPATIBLE_KEY',
                        '--workspace', directory)
        factory.assert_called_once_with('local-test', 'http://localhost:1234/v1', 'test-only')
