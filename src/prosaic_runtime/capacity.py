"""Shared, non-secret quota cooldowns; no model calls or credential inspection."""
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import tempfile
from threading import RLock
import time


class CapacityStore:
    """In-memory by default; optional atomic, flock-protected local file store.

    Use the same quota key for profiles sharing provider/account/quota buckets.
    Unknown and expired cooldowns are eligible, never a reservation of capacity.
    File-backed stores require POSIX advisory locks and a trusted local directory.
    """
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else None
        self._lock = RLock()
        self._buckets = {}

    @contextmanager
    def _transaction(self, write=False, *, deadline=None, cancelled=None):
        def check():
            if cancelled is not None and cancelled():
                raise InterruptedError('capacity wait cancelled')
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('capacity wait deadline exceeded')
        check()
        while not self._lock.acquire(blocking=False):
            check()
            time.sleep(.01)
        try:
            if self.path is None:
                yield self._buckets
                return
            import fcntl
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self.path.with_name(self.path.name + '.lock')
            with os.fdopen(os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600), 'a') as lock:
                while True:
                    check()
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        time.sleep(.01)
                try:
                    buckets = self._read()
                    yield buckets
                    if write:
                        fd, temporary = tempfile.mkstemp(prefix='.' + self.path.name, dir=self.path.parent)
                        try:
                            with os.fdopen(fd, 'w', encoding='utf-8') as target:
                                json.dump({'version': 1, 'buckets': buckets}, target, allow_nan=False)
                                target.flush()
                                os.fsync(target.fileno())
                            os.replace(temporary, self.path)
                        finally:
                            if os.path.exists(temporary):
                                os.unlink(temporary)
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
        finally:
            self._lock.release()

    def _read(self):
        if not self.path.exists():
            return {}
        try:
            if self.path.stat().st_size > 1048576:
                raise ValueError('oversized')
            raw = json.loads(self.path.read_text(encoding='utf-8'))
            if set(raw) != {'version', 'buckets'} or raw['version'] != 1 or not isinstance(raw['buckets'], dict):
                raise ValueError('invalid structure')
            for key, value in raw['buckets'].items():
                if not isinstance(key, str) or not isinstance(value, dict):
                    raise ValueError('invalid bucket')
                if value.get('state') not in {'available', 'cooling_down'}:
                    raise ValueError('invalid state')
                if value['state'] == 'cooling_down':
                    self._until(value.get('until'))
                    if value.get('reason') not in {'rate_limit', 'usage_limit', 'unavailable'}:
                        raise ValueError('invalid reason')
            return raw['buckets']
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise ValueError('invalid capacity state file') from exc

    @staticmethod
    def _until(value):
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
            raise ValueError('capacity reset must be a finite epoch timestamp')

    def status(self, key, *, now=None, deadline=None, cancelled=None):
        now = time.time() if now is None else now
        with self._transaction(deadline=deadline, cancelled=cancelled) as buckets:
            value = dict(buckets.get(key, {'state': 'unknown'}))
        if value['state'] == 'cooling_down' and value['until'] <= now:
            return {'state': 'unknown'}
        return value

    def available(self, key, *, now=None, deadline=None, cancelled=None):
        return self.status(key, now=now, deadline=deadline, cancelled=cancelled)['state'] != 'cooling_down'

    def mark_unavailable(self, key, *, reason, until, deadline=None, cancelled=None):
        self._until(until)
        if not isinstance(key, str) or not key or reason not in {'rate_limit', 'usage_limit', 'unavailable'}:
            raise ValueError('invalid capacity bucket or reason')
        with self._transaction(write=True, deadline=deadline, cancelled=cancelled) as buckets:
            previous = buckets.get(key, {})
            until = max(until, previous.get('until', 0))
            buckets[key] = {'state': 'cooling_down', 'reason': reason, 'until': until}

    def mark_available(self, key):
        """Explicit host refresh/reset; successful tasks do not clear cooldowns."""
        if not isinstance(key, str) or not key:
            raise ValueError('invalid capacity bucket')
        with self._transaction(write=True) as buckets:
            buckets[key] = {'state': 'available'}
