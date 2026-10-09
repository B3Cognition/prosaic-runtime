"""Host response guards shared by native JSON and stream readers."""
import time

from .events import check_cancelled


class LimitExceeded(ValueError):
    pass


def set_response_timeout(response, remaining):
    """Find urllib's socket through bounded/accounting and HTTPError wrappers."""
    pending, seen = [response], set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        sock = getattr(current, '_sock', None)
        if sock is not None:
            sock.settimeout(max(0.001, remaining))
            return
        pending.extend(getattr(current, name, None) for name in ('response', 'fp', 'raw'))


class BoundedNativeStream:
    """Assemble lines with deadline/cancellation checks between socket reads."""
    def __init__(self, response, limit, deadline):
        self.response, self.remaining, self.deadline = response, limit, deadline
        self.buffer = bytearray()
        self.eof = False

    def __getattr__(self, name):
        return getattr(self.response, name)

    def readline(self):
        while True:
            check_cancelled()
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('native stream deadline')
            index = self.buffer.find(b'\n')
            if index >= 0 or self.eof:
                size = index + 1 if index >= 0 else len(self.buffer)
                line = bytes(self.buffer[:size])
                del self.buffer[:size]
                capture = getattr(self.response, 'capture_line', None)
                if line and callable(capture):
                    capture(line)
                return line
            set_response_timeout(self.response, remaining)
            reader = getattr(self.response, 'read_chunk', None) or self.response.read1
            chunk = reader(min(65536, self.remaining + 1))
            self.remaining -= len(chunk)
            if self.remaining < 0:
                raise LimitExceeded('response exceeds max_response_bytes')
            self.buffer.extend(chunk)
            if not chunk:
                self.eof = True
