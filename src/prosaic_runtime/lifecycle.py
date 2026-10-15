"""Cooperative admission and drain for synchronous execution and persistence.

Hosts own signal handlers and must wait before closing stores or cancelling work.
A timeout reports unfinished work; it never cancels or kills admitted execution.
"""
from contextlib import contextmanager
from functools import wraps
import math
import threading
import time


class Draining(RuntimeError):
    """New root execution was refused because its lifecycle is draining."""


class ExecutionLifecycle:
    def __init__(self):
        self._condition = threading.Condition()
        self._local = threading.local()
        self._draining = False
        self._active = 0

    @property
    def draining(self):
        with self._condition:
            return self._draining

    @property
    def active(self):
        with self._condition:
            return self._active

    @contextmanager
    def work(self):
        """Admit synchronous work; nested work on the admitted thread may finish."""
        depth = getattr(self._local, 'depth', 0)
        with self._condition:
            if self._draining and not depth:
                raise Draining('service-draining')
            self._active += 1
        self._local.depth = depth + 1
        try:
            yield
        finally:
            self._local.depth = depth
            with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def begin(self):
        """Permanently fence new root work. Does not cancel admitted work."""
        with self._condition:
            self._draining = True
            self._condition.notify_all()

    def wait(self, timeout=None):
        """Fence and wait through persistence; return False on an optional timeout.

        Call from a separate supervisor thread, never inside admitted work.
        Nested admission is thread-scoped, not inherited by new worker threads.
        """
        if getattr(self._local, 'depth', 0):
            raise RuntimeError('cannot wait for own admitted execution')
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                   or not math.isfinite(timeout) or timeout < 0):
            raise ValueError('timeout must be finite and nonnegative')
        deadline = None if timeout is None else time.monotonic() + timeout
        self.begin()
        with self._condition:
            while self._active:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True


def tracked(method):
    """Track a synchronous operation on its owner's public lifecycle."""
    @wraps(method)
    def execute(self, *args, **kwargs):
        with self.lifecycle.work():
            return method(self, *args, **kwargs)
    return execute
