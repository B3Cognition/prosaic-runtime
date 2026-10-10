"""Actual dispatch and partial response reads honor the invocation boundary."""
import io
import json
import time
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

import pytest

from prosaic_runtime import EndpointConfig, RunPolicy
from prosaic_runtime.accounting import MemoryRecorder
from test_runtime import artifact, runtime, server


class Response(io.BytesIO):
    status = 200
    headers = Message()

    def __init__(self):
        super().__init__(json.dumps({'model': 'small', 'choices': [
            {'message': {'content': 'done'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5}}).encode())


@pytest.mark.parametrize('boundary,accounted', [('observer', False), ('observer', True), ('prepare', True)])
@pytest.mark.parametrize('stop', ['cancel', 'deadline'])
def test_callback_stop_prevents_actual_dispatch(monkeypatch, boundary, accounted, stop):
    clock = [100.0]
    stopped, requests, events = [], [], []

    def stop_execution():
        stopped.append(True)
        if stop == 'deadline':
            clock[0] += 2

    class Recorder(MemoryRecorder):
        def prepare(self, intent):
            super().prepare(intent)
            if boundary == 'prepare':
                stop_execution()

    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            return Response()

    def observer(event):
        events.append(event)
        if boundary == 'observer' and event['event'] == 'provider_request_started':
            stop_execution()

    recorder = Recorder() if accounted else None
    monkeypatch.setattr('time.monotonic', lambda: clock[0])
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: Opener())
    result = runtime(accounting=recorder).run(artifact(), observer=observer,
        cancelled=lambda: stop == 'cancel' and bool(stopped),
        policy=RunPolicy(timeout_s=1, max_provider_requests=2))
    assert requests == []
    assert result.exit_code == 130 if stop == 'cancel' else result.exit_code != 0
    assert result.timed_out == (stop == 'deadline')
    assert result.metadata['invocation_budgets_v1']['provider_requests'] == 0
    completed = next(e for e in events if e['event'] == 'provider_request_completed')
    assert completed['outcome'] == ('cancelled' if stop == 'cancel' else 'timed_out')
    if accounted:
        ids = result.metadata['accounting_v1']['provider_call_ids']
        assert ids == list(recorder.intents) == list(recorder.observations)
        assert len(ids) == (1 if boundary == 'prepare' else 0)
        for observation in recorder.observations.values():
            assert observation['usage']['status'] == 'unknown'
            assert observation['outcome'] == ('cancelled' if stop == 'cancel' else 'failed')


@pytest.mark.parametrize('boundary', ['observer', 'prepare'])
def test_dispatch_timeout_is_refreshed_after_callback(monkeypatch, boundary):
    clock, timeouts = [100.0], []

    class Recorder(MemoryRecorder):
        def prepare(self, intent):
            super().prepare(intent)
            if boundary == 'prepare':
                clock[0] += 0.6

    class Opener:
        def open(self, request, timeout):
            timeouts.append(timeout)
            return Response()

    def observer(event):
        if boundary == 'observer' and event['event'] == 'provider_request_started':
            clock[0] += 0.6

    monkeypatch.setattr('time.monotonic', lambda: clock[0])
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: Opener())
    result = runtime(accounting=Recorder()).run(artifact(), observer=observer, policy=RunPolicy(timeout_s=1))
    assert result.exit_code == 0
    assert timeouts == pytest.approx([0.4])


@pytest.fixture
def drip_server(request):
    requests, stop, sent = [], Event(), Event()
    status = getattr(request, 'param', 200)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(request)
            value = json.dumps({'model': 'small', 'choices': [
                {'message': {'content': 'done'}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5}})
            if status != 200:
                value = json.dumps({'error': 'fixture error ' * 25})
            streaming = status == 200 and request.get('stream', False)
            body = ('data: ' + value + '\n\ndata: [DONE]\n\n' if streaming else value).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'text/event-stream' if streaming else 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                for start in range(0, len(body), 8):
                    self.wfile.write(body[start:start + 8])
                    self.wfile.flush()
                    sent.set()
                    if stop.wait(0.025):
                        return
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{http.server_port}/v1', requests, sent
    finally:
        stop.set()
        http.shutdown()
        thread.join(timeout=2)
        http.server_close()


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('accounted', [False, True])
@pytest.mark.parametrize('stop', ['deadline', 'cancel'])
def test_partial_openai_body_obeys_boundary(drip_server, streaming, accounted, stop):
    url, requests, sent = drip_server
    recorder = MemoryRecorder() if accounted else None
    runner = runtime(EndpointConfig(url, 'small', features={'streaming': streaming}), accounting=recorder)
    started = time.monotonic()
    result = runner.run(artifact(), policy=RunPolicy(timeout_s=0.1 if stop == 'deadline' else 2),
        cancelled=lambda: stop == 'cancel' and sent.is_set() and time.monotonic() - started >= 0.08)
    elapsed = time.monotonic() - started
    assert result.exit_code == 130 if stop == 'cancel' else result.exit_code != 0
    assert result.timed_out == (stop == 'deadline')
    assert elapsed < 0.35  # Reading the complete drip-fed body takes over 0.6s.
    assert len(requests) == 1
    if accounted:
        assert len(recorder.observations) == 1
        observation = next(iter(recorder.observations.values()))
        assert observation['usage']['status'] == 'unknown'
        assert observation['outcome'] == ('cancelled' if stop == 'cancel' else 'failed')


def test_sse_byte_limit_preserves_evidence_from_prior_complete_line(server):
    url, requests, responses = server
    first = b'data: ' + json.dumps({'model': 'small', 'choices': [
        {'delta': {'content': 'done'}, 'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 5, 'completion_tokens': 2, 'total_tokens': 7}}).encode() + b'\n'
    responses.append(('text/event-stream', first + b'\n:' + b'padding' * 100 + b'\n\n'))
    recorder = MemoryRecorder()
    result = runtime(EndpointConfig(url, 'small', max_response_bytes=len(first) + 20),
        accounting=recorder).run(artifact())
    assert len(requests) == 1 and result.exit_code != 0
    assert result.metadata['failure_reason'] == 'budget_exceeded'
    observation = next(iter(recorder.observations.values()))
    assert observation['usage']['total_tokens'] == 7
    assert observation['usage']['status'] == 'reported'
    assert observation['outcome'] == 'failed'


def test_sse_byte_limit_rejects_one_extra_byte_after_exact_cap(monkeypatch):
    first = b'data: {"choices":[{"delta":{"content":"done"},"finish_reason":"stop"}]}\n\n'

    class SplitResponse(io.BytesIO):
        status = 200
        headers = {'Content-Type': 'text/event-stream'}

        def read1(self, size):
            return super().read1(min(size, len(first)))

    class Opener:
        def open(self, request, timeout):
            return SplitResponse(first + b'X')

    monkeypatch.setattr('urllib.request.build_opener', lambda *args: Opener())
    result = runtime(EndpointConfig('http://fixture.invalid/v1', 'small',
        max_response_bytes=len(first))).run(artifact())
    assert result.exit_code != 0
    assert result.metadata['failure_reason'] == 'budget_exceeded'


def test_openai_buffered_sse_fallback_keeps_legacy_admission(server):
    url, requests, responses = server
    body = b'data: {"choices":[{"delta":{"content":"done"},"finish_reason":"stop"}],"usage":{"total_tokens":7}}\n\ndata: [DONE]\n\n'
    responses.append(('application/json', body))
    recorder = MemoryRecorder()
    result = runtime(EndpointConfig(url, 'small'), accounting=recorder).run(artifact())
    assert len(requests) == 1 and result.exit_code == 0
    assert result.stdout == 'done' and result.token_usage == 7
    assert next(iter(recorder.observations.values()))['usage']['total_tokens'] == 7


@pytest.mark.parametrize('drip_server', [503], indirect=True)
@pytest.mark.parametrize('tools', [False, True])
@pytest.mark.parametrize('accounted', [False, True])
@pytest.mark.parametrize('cap', [None, 20])
def test_openai_drip_http_error_returns_timeout_result(drip_server, tools, accounted, cap):
    url, requests, _ = drip_server
    recorder, events = MemoryRecorder() if accounted else None, []
    runner = runtime(EndpointConfig(url, 'small', features={'streaming': False}), accounting=recorder)
    started = time.monotonic()
    result = runner.run(artifact('read' if tools else ''), observer=events.append,
        policy=RunPolicy(timeout_s=0.1, max_provider_requests=1, max_reported_tokens=cap,
            allowed_tools=frozenset({'read_file'}) if tools else frozenset()))
    assert time.monotonic() - started < 0.35
    assert result.exit_code != 0 and result.timed_out
    assert result.metadata['provider_error_code'] == 'timeout'
    assert result.token_usage is None
    assert len(requests) == 1
    budget = result.metadata['invocation_budgets_v1']
    assert budget['provider_requests'] == 1 and budget['tool_calls'] == 0
    assert budget['reported_tokens'] == 0
    if cap is not None:
        assert result.metadata['failure_reason'] == 'usage_unknown'
        assert budget['usage_complete'] is False
    assert events[-1]['event'] == 'invocation_completed'
    assert events[-1]['outcome'] == 'timed_out'
    if accounted:
        ids = result.metadata['accounting_v1']['provider_call_ids']
        assert len(ids) == 1 and ids == list(recorder.intents) == list(recorder.observations)
        observation = recorder.observations[ids[0]]
        assert observation['outcome'] == 'failed'
        assert observation['usage']['status'] == 'unknown'
