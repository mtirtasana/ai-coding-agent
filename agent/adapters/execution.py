"""Explicit local commands with bounded capture; this is not a sandbox."""
import json
import math
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from agent.core.types import ToolResult


class LocalTestRunner:
    def __init__(self, root: Path, command, timeout=60, output_limit=65536):
        self.root = root.resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Test working directory must be a directory.")
        if (not isinstance(command, (list, tuple)) or not command or not command[0]
                or any(not isinstance(arg, str) or '\x00' in arg for arg in command)):
            raise ValueError("Test command must be a nonempty argument list.")
        if not math.isfinite(timeout) or timeout <= 0 or type(output_limit) is not int or output_limit < 1:
            raise ValueError("Timeout and output limit must be positive and finite.")
        self.command, self.timeout, self.output_limit = tuple(command), timeout, output_limit

    @staticmethod
    def _stop(process):
        if os.name == 'nt':
            try:
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
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
        started = time.monotonic()
        process = subprocess.Popen(self.command, cwd=self.root, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=False,
                                   start_new_session=os.name != 'nt',
                                   creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
        captured = bytearray()
        done = threading.Event()
        truncated = False

        def drain():
            nonlocal truncated
            try:
                while True:
                    chunk = process.stdout.read(8192)
                    if not chunk:
                        break
                    remaining = max(0, self.output_limit - len(captured))
                    captured.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        truncated = True
            finally:
                done.set()

        reader = threading.Thread(target=drain, daemon=True)
        timed_out = False
        try:
            reader.start()
            while process.poll() is None or not done.is_set():
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
        return {'passed': process.returncode == 0 and not timed_out,
                'exit_code': process.returncode, 'output': bytes(captured).decode('utf-8', errors='replace'),
                'elapsed_seconds': round(time.monotonic() - started, 6), 'timed_out': timed_out,
                'output_truncated': truncated, 'capture_complete': done.is_set()}

    def as_tool(self):
        result = self.run()
        return ToolResult(result['passed'], json.dumps(result, ensure_ascii=False))
