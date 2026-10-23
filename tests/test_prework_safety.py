from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
from threading import Thread
import time

import pytest
from prosaic_runtime import EndpointConfig, ProsaicArtifact, ProsaicRuntime, RunPolicy, RuntimeConfig
from test_runtime import completion


@pytest.fixture
def rejection_server():
    requests, responses = [], []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            response = responses.pop(0)
            status, body, delay = response[:3]
            time.sleep(delay)
            self.send_response(status)
            self.send_header('Content-Type', response[3] if len(response) > 3 else 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
        def log_message(self, *args):
            pass
    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{http.server_port}/v1', requests, responses
    finally:
        http.shutdown()
        thread.join(timeout=2)
        http.server_close()


def api(url, *, tools=frozenset()):
    return ProsaicRuntime(RuntimeConfig({'local': EndpointConfig(url, 'test', features={'streaming': False})},
                                      {}, 'local', tools))


def artifact(tools='none'):
    return ProsaicArtifact('subagents/task.md', 'subagent', {'tools': tools}, 'Answer.')


def test_initial_api_429_has_transport_owned_prework_proof(rejection_server):
    url, requests, responses = rejection_server
    responses.append((429, b'{"error":"capacity"}', 0))
    result = api(url).run(artifact(), policy=RunPolicy(max_provider_requests=2, max_tool_calls=0))
    assert result.exit_code != 0 and len(requests) == 1
    assert result.metadata.get('prework_failure_v1') == {'safe_to_fallback': True, 'reason': 'rate_limit'}


def test_connection_refusal_has_preacceptance_proof():
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1', 0))
        port = reserved.getsockname()[1]
    # Darwin can drop packets to a bound, non-listening socket. Closing it gives
    # a real TCP refusal instead of accidentally testing a connection timeout.
    result = api(f'http://127.0.0.1:{port}/v1').run(artifact(), policy=RunPolicy(timeout_s=1))
    assert result.metadata.get('prework_failure_v1') == {'safe_to_fallback': True, 'reason': 'unavailable'}


@pytest.mark.parametrize('status,delay', [(401, 0), (503, 0), (200, .15)])
def test_auth_server_errors_and_timeout_never_prove_prework(rejection_server, status, delay):
    url, _, responses = rejection_server
    responses.append((status, b'bad', delay))
    result = api(url).run(artifact(), policy=RunPolicy(timeout_s=.07))
    assert result.exit_code != 0
    assert 'prework_failure_v1' not in result.metadata


def test_tool_then_429_is_not_safe_to_repeat(rejection_server, tmp_path):
    url, requests, responses = rejection_server
    call = {'id': 'write', 'type': 'function', 'function': {'name': 'write_file',
        'arguments': json.dumps({'path': 'out', 'content': 'effect'})}}
    responses.extend([(200, completion('', [call])[1], 0), (429, b'limited', 0)])
    result = api(url, tools=frozenset({'write_file'})).run(artifact(['write_file']), cwd=tmp_path,
        policy=RunPolicy(allowed_tools=frozenset({'write_file'}), write_paths=('out',),
                         max_provider_requests=3, max_tool_calls=1))
    assert (tmp_path / 'out').read_text() == 'effect' and len(requests) == 2
    assert result.exit_code != 0 and 'prework_failure_v1' not in result.metadata


def test_required_model_first_tool_has_not_executed_on_initial_rejection(rejection_server, tmp_path):
    url, requests, responses = rejection_server
    responses.append((429, b'limited', 0))
    (tmp_path / 'input').write_text('source')
    result = api(url, tools=frozenset({'read_file'})).run(artifact(['read_file']), cwd=tmp_path,
        policy=RunPolicy(allowed_tools=frozenset({'read_file'}), read_roots=('.',), initial_tool='read_file',
                         max_provider_requests=2, max_tool_calls=2))
    # A required first model tool is not a host pre-dispatch tool invocation.
    # An initial 429 still rejects before the model can satisfy that requirement.
    assert len(requests) == 1
    assert result.metadata.get('prework_failure_v1') == {'safe_to_fallback': True, 'reason': 'rate_limit'}


def test_strict_unknown_usage_failure_does_not_authorize_fallback(rejection_server):
    url, _, responses = rejection_server
    responses.append((429, b'limited', 0))
    result = api(url).run(artifact(), policy=RunPolicy(max_provider_requests=2, max_tool_calls=0,
                                                    max_reported_tokens=100))
    assert result.exit_code != 0
    assert result.metadata.get('failure_reason') == 'usage_unknown'
    assert 'prework_failure_v1' not in result.metadata


def test_native_exhaustion_exposes_positive_prework_proof(tmp_path):
    from prosaic_runtime import NativePolicy
    from test_native_runtime import setup_runtime
    r = setup_runtime(tmp_path, same_quota=True)
    result = r.run(artifact(), cwd=tmp_path, policy=NativePolicy())
    assert result.metadata['failure_reason'] == 'capacity_unavailable'
    assert result.metadata.get('prework_failure_v1') == {
        'safe_to_fallback': True, 'reason': 'capacity_unavailable'}


@pytest.mark.parametrize('failure', ['connection', 'http'])
def test_accepted_stream_callback_error_is_not_transport_rejection(rejection_server, failure):
    import io
    import urllib.error
    url, _, responses = rejection_server
    body = ('data: ' + json.dumps({'choices': [{'index': 0, 'delta': {'content': 'accepted work'},
        'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 1, 'completion_tokens': 2, 'total_tokens': 3}})
        + '\n\ndata: [DONE]\n\n').encode()
    responses.append((200, body, 0, 'text/event-stream'))
    runtime = ProsaicRuntime(RuntimeConfig({'local': EndpointConfig(url, 'test', features={'streaming': True})}, {}, 'local'))
    def fail(event):
        if event['event'] == 'text_delta':
            if failure == 'http':
                raise urllib.error.HTTPError(url, 429, 'callback', {}, io.BytesIO(b'callback'))
            raise urllib.error.URLError(ConnectionRefusedError('callback'))
    result = runtime.run(artifact(), on_event=fail,
        policy=RunPolicy(max_provider_requests=2, max_tool_calls=0))
    assert result.exit_code != 0
    assert 'prework_failure_v1' not in result.metadata
