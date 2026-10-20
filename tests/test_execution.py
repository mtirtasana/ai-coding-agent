import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.adapters.execution import LocalTestRunner
from agent.adapters.tools import workspace_tools
from agent.core.policy import ModeToolExecutor
from agent.core.types import ToolCall
from agent.files import Workspace


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def runner(self, code, **kwargs):
        return LocalTestRunner(self.root, [sys.executable, '-c', code], **kwargs)

    def test_pass_failure_and_working_directory(self):
        self.assertTrue(self.runner("from pathlib import Path; Path('cwd.txt').write_text('ok'); print('passed')").run()['passed'])
        self.assertEqual((self.root / 'cwd.txt').read_text(), 'ok')
        result = self.runner("import sys; print('failed', file=sys.stderr); sys.exit(7)").run()
        self.assertFalse(result['passed'])
        self.assertEqual(result['exit_code'], 7)
        self.assertIn('failed', result['output'])

    def test_output_limit_continues_draining(self):
        result = self.runner("import sys; sys.stdout.write('x'*200000); sys.stderr.write('y'*200000)",
                             output_limit=1024).run()
        self.assertTrue(result['passed'])
        self.assertEqual(len(result['output']), 1024)
        self.assertTrue(result['output_truncated'])
        self.assertTrue(result['capture_complete'])

    def test_timeout(self):
        result = self.runner('import time; time.sleep(20)', timeout=0.15).run()
        self.assertTrue(result['timed_out'])
        self.assertFalse(result['passed'])
        self.assertLess(result['elapsed_seconds'], 5)

    @unittest.skipIf(os.name == 'nt', 'POSIX process group assertion')
    def test_timeout_stops_descendants(self):
        code = ("import subprocess, sys, time; "
                "subprocess.Popen([sys.executable, '-c', \"import time; from pathlib import Path; "
                "time.sleep(0.6); Path('escaped.txt').write_text('bad')\"]); time.sleep(20)")
        self.runner(code, timeout=0.15).run()
        time.sleep(0.7)
        self.assertFalse((self.root / 'escaped.txt').exists())

    def test_interrupt_stops_process(self):
        real_popen = subprocess.Popen
        processes = []
        def popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process
        with patch('agent.adapters.execution.subprocess.Popen', side_effect=popen), \
                patch('agent.adapters.execution.time', wraps=time) as timer:
            timer.sleep.side_effect = KeyboardInterrupt
            # stdout closes immediately, leaving the process alive and entering sleep.
            with self.assertRaises(KeyboardInterrupt):
                self.runner('import os, time; os.close(1); os.close(2); time.sleep(20)').run()
        self.assertIsNotNone(processes[0].poll())

    def test_tool_unavailable_without_opt_in_and_denied_in_plan(self):
        workspace = Workspace(self.root)
        self.assertNotIn('run_tests', {s.name for s in workspace_tools(workspace).specifications()})
        runner = self.runner('import sys; sys.exit(3)')
        registry = workspace_tools(workspace, test_runner=runner)
        with patch.object(runner, 'run', wraps=runner.run) as run:
            denied = ModeToolExecutor(registry, 'plan').execute(ToolCall('a', 'run_tests', '{}'))
            self.assertFalse(denied.ok)
            run.assert_not_called()
        result = registry.execute(ToolCall('b', 'run_tests', '{}'))
        self.assertFalse(result.ok)
        self.assertEqual(json.loads(result.content)['exit_code'], 3)

    def test_no_shell_expansion_and_configuration_validation(self):
        result = LocalTestRunner(self.root, [sys.executable, '-c', 'import sys; print(sys.argv[1])',
                                            '$(touch injected); echo x']).run()
        self.assertTrue(result['passed'])
        self.assertIn('$(touch injected)', result['output'])
        self.assertFalse((self.root / 'injected').exists())
        for timeout in [0, -1, float('inf'), float('nan')]:
            with self.assertRaises(ValueError):
                self.runner('pass', timeout=timeout)
