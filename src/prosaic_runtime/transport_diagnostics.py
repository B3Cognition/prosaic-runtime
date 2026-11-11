"""Opt-in bounded HTTP observations; never response or accounting evidence."""
import hashlib
import math
import re
import time
from urllib.error import HTTPError

_TIMES = frozenset({'dispatch_ms', 'headers_ms', 'first_byte_ms', 'last_byte_ms', 'completed_ms'})
_COUNTS = frozenset({'body_bytes', 'read_calls', 'content_length'})
_ENUMS = {
    'transfer_encoding': {'absent', 'chunked', 'other'},
    'connection': {'absent', 'close', 'keep-alive', 'other'},
    'content_type': {'absent', 'json', 'sse', 'other'},
    'content_encoding': {'absent', 'identity', 'gzip', 'other'},
    'exception_stage': {None, 'headers', 'body', 'after-eof'},
    'exception_kind': {None, 'timeout', 'incomplete-read', 'transport', 'other'},
    'schema_enforcement': {'not-reported', 'unavailable'},
}
_FIELDS = _TIMES | _COUNTS | set(_ENUMS) | {'correlation_id', 'body_sha256', 'eof', 'warning_present', 'server_request_id_sha256'}


def safe_summary(value):
    """Copy a closed scalar record; reject injected headers, text and nonfinite data."""
    if type(value) is not dict or set(value) != _FIELDS:
        return None
    for key in _TIMES:
        n = value[key]
        if n is not None and (type(n) not in (int, float) or not 0 <= n <= 2**63-1 or not math.isfinite(n)):
            return None
    for key in _COUNTS:
        n = value[key]
        if n is None and key == 'content_length':
            continue
        if type(n) is not int or not 0 <= n <= 2**63-1:
            return None
    for key, allowed in _ENUMS.items():
        if value[key] is not None and type(value[key]) is not str:
            return None
        if value[key] not in allowed:
            return None
    if type(value['eof']) is not bool or type(value['body_sha256']) is not str or not re.fullmatch('[0-9a-f]{64}', value['body_sha256']):
        return None
    if type(value['warning_present']) is not bool:
        return None
    server_id = value['server_request_id_sha256']
    if server_id is not None and (type(server_id) is not str or not re.fullmatch('[0-9a-f]{64}', server_id)):
        return None
    cid = value['correlation_id']
    if cid is not None and (type(cid) is not str or not re.fullmatch('[0-9a-f]{32}', cid)):
        return None
    return dict(value)


class TransportTrace:
    def __init__(self, started):
        self.started = started
        self.hash = hashlib.sha256()
        self.phase = 'headers'
        self.value = dict.fromkeys(_FIELDS)
        self.value.update(body_bytes=0, read_calls=0, eof=False, warning_present=False, schema_enforcement='not-reported',
                          **{k: 'absent' for k in ('transfer_encoding', 'connection', 'content_type', 'content_encoding')})

    def elapsed(self):
        return max(0, (time.monotonic() - self.started) * 1000)

    def dispatched(self, request):
        cid = request.get_header('X-correlation-id')
        self.value['correlation_id'] = cid if type(cid) is str and re.fullmatch('[0-9a-f]{32}', cid) else None
        self.value['dispatch_ms'] = self.elapsed()

    def headers(self, response):
        from .response_headers import header_signals
        self.value.update(header_signals(response))
        self.value['headers_ms'] = self.elapsed()
        headers = getattr(response, 'headers', {})
        raw = headers.get('Content-Length')
        if type(raw) is str and re.fullmatch('[0-9]{1,19}', raw) and int(raw) <= 2**63-1:
            self.value['content_length'] = int(raw)
        for key, header in [('transfer_encoding', 'Transfer-Encoding'), ('connection', 'Connection'), ('content_type', 'Content-Type'), ('content_encoding', 'Content-Encoding')]:
            raw = headers.get(header)
            normalized = raw.strip().lower() if type(raw) is str and len(raw) <= 128 else None
            if key == 'content_type':
                normalized = {'application/json': 'json', 'text/event-stream': 'sse'}.get((normalized or '').split(';')[0], 'other')
            self.value[key] = 'absent' if raw is None else normalized if normalized in _ENUMS[key] else 'other'
        self.phase = 'body'

    def failed(self, error, stage=None):
        if self.value['exception_kind'] is not None:
            return
        from http.client import IncompleteRead
        self.value['exception_kind'] = ('timeout' if isinstance(error, TimeoutError) else
                                       'incomplete-read' if isinstance(error, IncompleteRead) else
                                       'transport' if isinstance(error, OSError) else 'other')
        self.value['exception_stage'] = stage or self.phase

    def received(self, raw):
        if raw:
            now = self.elapsed()
            if self.value['first_byte_ms'] is None:
                self.value['first_byte_ms'] = now
            self.value['last_byte_ms'] = now
            self.value['body_bytes'] += len(raw)
            self.hash.update(raw)
        else:
            self.value['eof'] = True
            self.phase = 'after-eof'

    def summary(self):
        return {**self.value, 'body_sha256': self.hash.hexdigest(), 'completed_ms': self.elapsed()}


class DiagnosticResponse:
    def __init__(self, response, trace):
        self.response, self.trace = response, trace

    def __getattr__(self, name):
        value = getattr(self.response, name)
        if name in {'read', 'read1', 'readline'} and callable(value):
            def read(*args, **kwargs):
                self.trace.value['read_calls'] += 1
                try:
                    raw = value(*args, **kwargs)
                except BaseException as error:
                    self.trace.failed(error, 'body')
                    raise
                self.trace.received(raw)
                return raw
            return read
        return value

    def __enter__(self):
        self.response.__enter__()
        return self

    def __exit__(self, *args):
        return self.response.__exit__(*args)


class DiagnosticOpener:
    def __init__(self, opener, trace):
        self.opener, self.trace = opener, trace

    def open(self, request, *, timeout):
        self.trace.dispatched(request)
        try:
            response = self.opener.open(request, timeout=timeout)
        except HTTPError as error:
            self.trace.headers(error)
            raise
        except BaseException as error:
            self.trace.failed(error, 'headers')
            raise
        self.trace.headers(response)
        return DiagnosticResponse(response, self.trace)
