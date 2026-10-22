"""Explicit local commands with bounded capture; this is not a sandbox."""
import json
import hashlib
import math
import os
import signal
# Explicit host-command adapter; see docs/security-checks.md.
import subprocess  # nosec B404
import threading
import time
from pathlib import Path

from agent.core.cancellation import CancellationToken
from agent.core.types import ToolResult


class LocalCommandRunner:
    def __init__(self, root: Path, command, timeout=60, output_limit=65536, *, check='test', cancellation=None):
        self.cancellation = cancellation or CancellationToken()
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Verification working directory must be a directory.")
        if (not isinstance(command, (list, tuple)) or not command or not command[0]
                or any(not isinstance(arg, str) or '\x00' in arg for arg in command)):
            raise ValueError("Verification command must be a nonempty argument list.")
        if not math.isfinite(timeout) or timeout <= 0 or type(output_limit) is not int or output_limit < 1:
            raise ValueError("Timeout and output limit must be positive and finite.")
        self.command, self.timeout, self.output_limit = tuple(command), timeout, output_limit
        if check not in {'test', 'lint', 'lint_fix', 'security', 'dependency_audit'}:
            raise ValueError('Unknown verification profile.')
        self.check = check
        self.last_raw = None

    @staticmethod
    def _stop(process):
        if os.name == 'nt':
            try:
                # Resolve the fixed OS utility independently of the workspace/PATH.
                taskkill = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'taskkill.exe'
                subprocess.run([str(taskkill), '/PID', str(process.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)  # nosec B603
            except (OSError, subprocess.TimeoutExpired):
                pass
            if process.poll() is None:
                process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def run(self):
        self.last_raw = None
        started = time.monotonic()
        # Only host-configured argv reaches this call; model tools cannot supply commands.
        with self.cancellation.guard():
            process = subprocess.Popen(self.command, cwd=self.root, stdin=subprocess.DEVNULL,  # nosec B603
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=False,
                                       start_new_session=os.name != 'nt',
                                       creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
            self.cancellation.track(process)
        captured = bytearray()
        done = threading.Event()
        truncated = False
        capture_error = None

        def drain():
            nonlocal truncated, capture_error
            try:
                while True:
                    chunk = process.stdout.read(8192)
                    if not chunk:
                        break
                    remaining = max(0, self.output_limit - len(captured))
                    captured.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        truncated = True
            except Exception as exc:
                capture_error = f'{type(exc).__name__}: {exc}'
            finally:
                done.set()

        reader = threading.Thread(target=drain, daemon=True)
        timed_out = False
        try:
            reader.start()
            while process.poll() is None or not done.is_set():
                if capture_error is not None or self.cancellation.cancelled:
                    self._stop(process)
                    break
                remaining = self.timeout - (time.monotonic() - started)
                if remaining <= 0:
                    timed_out = True
                    self._stop(process)
                    break
                done.wait(min(0.05, remaining))
                if done.is_set() and process.poll() is None:
                    # EOF can precede process termination; avoid a busy loop.
                    time.sleep(min(0.01, max(0, remaining)))
            process.wait(timeout=5)
        except BaseException:
            self._stop(process)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            raise
        finally:
            if reader.ident is not None:
                reader.join(timeout=2)
            if done.is_set() or reader.ident is None:
                process.stdout.close()
            if process.poll() is not None:
                self.cancellation.release(process)
        capture_complete = done.is_set() and capture_error is None
        self.last_raw = bytes(captured)
        return {'check': self.check, 'passed': process.returncode == 0 and not timed_out and capture_complete and not self.cancellation.cancelled,
                'exit_code': process.returncode, 'output': self.last_raw.decode('utf-8', errors='replace'),
                'captured_bytes': len(self.last_raw), 'raw_sha256': hashlib.sha256(self.last_raw).hexdigest(),
                'elapsed_seconds': round(time.monotonic() - started, 6), 'timed_out': timed_out,
                'output_truncated': truncated, 'capture_complete': capture_complete,
                'capture_error': capture_error, 'cancelled': self.cancellation.cancelled}

    def as_tool(self):
        result = self.run()
        return ToolResult(result['passed'], json.dumps(result, ensure_ascii=False))


# Preserve the original public import used by integrations and the evaluator.
LocalTestRunner = LocalCommandRunner


class LintFixRunner:
    """Run the explicitly configured fixer, then recheck lint; never install tools."""
    def __init__(self, fixer, lint):
        if fixer.root != lint.root:
            raise ValueError('Fix and lint commands must use the same workspace.')
        self.fixer, self.lint = fixer, lint
        self.last_raw = None

    def as_tool(self):
        self.last_raw = None
        fixed = self.fixer.run()
        linted = None if fixed['timed_out'] else self.lint.run()
        result = {'check': 'lint_fix',
                  'passed': fixed['passed'] and linted is not None and linted['passed'],
                  'fix': fixed, 'lint': linted, 'requires_test_rerun': True}
        self.last_raw = {'fix': {'raw': getattr(self.fixer, 'last_raw', None), 'command': self.fixer.command},
                         'lint': {'raw': getattr(self.lint, 'last_raw', None) if linted is not None else None,
                                  'command': self.lint.command}}
        return ToolResult(result['passed'], json.dumps(result, ensure_ascii=False))
