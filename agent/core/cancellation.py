"""Cooperative cancellation with a barrier for owned command processes."""
import threading
import time
from contextlib import contextmanager


class RunCancelled(RuntimeError):
    pass


class CancellationToken:
    def __init__(self):
        self._event = threading.Event()
        self._condition = threading.Condition(threading.RLock())
        self._processes = {}

    @property
    def cancelled(self):
        return self._event.is_set()

    def check(self):
        if self.cancelled:
            raise RunCancelled('Run cancelled; accepted edits may remain.')

    @contextmanager
    def guard(self):
        with self._condition:
            self.check()
            yield

    def cancel(self):
        with self._condition:
            self._event.set()
            self._condition.notify_all()

    def track(self, process):
        with self._condition:
            self._processes[id(process)] = process

    def release(self, process):
        with self._condition:
            self._processes.pop(id(process), None)
            self._condition.notify_all()

    def wait_for_cleanup(self, timeout=8):
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._processes:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True
