import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.adapters.execution import LocalCommandRunner, LocalTestRunner, LintFixRunner
from agent.adapters.tools import workspace_tools
from agent.core.policy import ModeToolExecutor
from agent.core.types import ToolCall
from agent.files import Workspace


class LintingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'sample.py').write_text('import os\nvalue = 1\n')
        self.workspace = Workspace(self.root, allow_write=True)
        self.lint = self.runner("from pathlib import Path; import sys; sys.exit(1 if Path('sample.py').read_text().startswith('import os') else 0)", 'lint')
        self.fixer = self.runner("from pathlib import Path; p=Path('sample.py'); p.write_text(p.read_text().replace('import os\\n',''))", 'lint_fix')

    def runner(self, code, check, **kwargs):
        return LocalCommandRunner(self.root, [sys.executable, '-c', code], check=check, **kwargs)

    def test_tests_and_lint_have_independent_results(self):
        tests = self.runner("print('tests pass')", 'test')
        registry = workspace_tools(self.workspace, test_runner=tests, lint_runner=self.lint)
        passed = registry.execute(ToolCall('t', 'run_tests', '{}'))
        failed = registry.execute(ToolCall('l', 'run_lint', '{}'))
        self.assertTrue(passed.ok)
        self.assertFalse(failed.ok)
        self.assertEqual(json.loads(passed.content)['check'], 'test')
        self.assertEqual(json.loads(failed.content)['check'], 'lint')
        self.assertEqual(json.loads(failed.content)['exit_code'], 1)
        self.assertIn('import os', (self.root / 'sample.py').read_text())
        self.assertIs(LocalTestRunner, LocalCommandRunner)

    def test_opt_in_fixes_recheck_lint_and_require_test_rerun(self):
        fixes = LintFixRunner(self.fixer, self.lint)
        registry = workspace_tools(self.workspace, lint_runner=self.lint, lint_fix_runner=fixes)
        result = registry.execute(ToolCall('f', 'run_lint_fix', '{}'))
        payload = json.loads(result.content)
        self.assertTrue(result.ok)
        self.assertTrue(payload['fix']['passed'])
        self.assertTrue(payload['lint']['passed'])
        self.assertTrue(payload['requires_test_rerun'])
        self.assertEqual((self.root / 'sample.py').read_text(), 'value = 1\n')

    def test_fix_requires_write_permission_and_is_never_read_only(self):
        fixes = LintFixRunner(self.fixer, self.lint)
        registry = workspace_tools(Workspace(self.root), lint_runner=self.lint, lint_fix_runner=fixes)
        self.assertNotIn('run_lint_fix', {s.name for s in registry.specifications()})
        registry = workspace_tools(self.workspace, lint_runner=self.lint, lint_fix_runner=fixes)
        planning = ModeToolExecutor(registry, 'plan')
        with patch.object(self.lint, 'run') as lint, patch.object(self.fixer, 'run') as fix:
            self.assertFalse(planning.execute(ToolCall('l', 'run_lint', '{}')).ok)
            self.assertFalse(planning.execute(ToolCall('f', 'run_lint_fix', '{}')).ok)
            lint.assert_not_called()
            fix.assert_not_called()

    def test_failed_fix_is_not_reported_as_success(self):
        failed = self.runner('import sys; sys.exit(2)', 'lint_fix')
        with patch.object(self.lint, 'run', wraps=self.lint.run) as lint:
            result = LintFixRunner(failed, self.lint).as_tool()
        self.assertFalse(result.ok)
        lint.assert_called_once()
        self.assertEqual(json.loads(result.content)['fix']['exit_code'], 2)

    def test_fix_timeout_stops_without_follow_up_command(self):
        timed = self.runner('import time; time.sleep(10)', 'lint_fix', timeout=0.1)
        with patch.object(self.lint, 'run') as lint:
            result = LintFixRunner(timed, self.lint).as_tool()
        payload = json.loads(result.content)
        self.assertFalse(result.ok)
        self.assertTrue(payload['fix']['timed_out'])
        self.assertIsNone(payload['lint'])
        lint.assert_not_called()

    def test_only_configured_profiles_are_exposed_and_arguments_are_rejected(self):
        registry = workspace_tools(self.workspace, lint_runner=self.lint)
        names = {s.name for s in registry.specifications()}
        self.assertIn('run_lint', names)
        self.assertNotIn('run_tests', names)
        self.assertNotIn('run_lint_fix', names)
        with patch.object(self.lint, 'run') as lint:
            result = registry.execute(ToolCall('l', 'run_lint', '{"command":"something else"}'))
            self.assertFalse(result.ok)
            lint.assert_not_called()
