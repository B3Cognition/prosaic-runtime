"""Native Messages integration through the public bounded Runtime API."""
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import json

import pytest

from prosaic_runtime import CustomTool, EndpointConfig, ProsaicRuntime, RunPolicy, RuntimeConfig
from prosaic_runtime.policy import BUILTIN_TOOLS
from test_runtime import artifact, completion


@pytest.fixture
def native_server():
    requests, responses = [], []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append({'path': self.path, 'headers': {k.lower(): v for k, v in self.headers.items()},
                             'body': json.loads(self.rfile.read(int(self.headers['Content-Length'])))})
            self.respond()

        def do_GET(self):
            requests.append({'path': self.path, 'headers': {k.lower(): v for k, v in self.headers.items()}, 'body': None})
            self.respond()

        def respond(self):
            status, content_type, body, headers = responses.pop(0)
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

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


def message(text='done', calls=(), stop=None, usage=None):
    return {'id': 'msg_test', 'type': 'message', 'role': 'assistant', 'model': 'claude-test',
            'content': ([{'type': 'text', 'text': text}] if text else []) + list(calls),
            'stop_reason': stop or ('tool_use' if calls else 'end_turn'), 'stop_sequence': None,
            'usage': usage if usage is not None else {'input_tokens': 5, 'output_tokens': 2}}


def response(value=None, **kwargs):
    return 200, 'application/json', json.dumps(value or message(**kwargs)).encode(), {}


def tool(name='read_file', call_id='t1', **arguments):
    return {'type': 'tool_use', 'id': call_id, 'name': name, 'input': arguments}


def native_runtime(url, *, features=None, custom_tools=None, allowed=BUILTIN_TOOLS, **kwargs):
    endpoint = EndpointConfig(url, 'claude-test', api_key_env='TEST_ANTHROPIC_KEY',
                              provider='anthropic', features={'streaming': False, **(features or {})})
    return ProsaicRuntime(RuntimeConfig({'native': endpoint}, {'fast': 'native'}, 'native', allowed),
                          custom_tools=custom_tools, **kwargs)


def test_native_text_headers_payload_and_usage(native_server):
    url, requests, responses = native_server
    responses.append(response())
    result = native_runtime(url).run(artifact(), env={'TEST_ANTHROPIC_KEY': 'fixture-key'})
    assert result.exit_code == 0 and result.stdout == 'done' and result.token_usage == 7
    assert result.metadata['provider'] == 'anthropic'
    assert result.metadata['raw_response_metadata']['stop_reason'] == 'end_turn'
    request = requests[0]
    assert request['path'] == '/v1/messages'
    assert request['headers']['x-api-key'] == 'fixture-key'
    assert request['headers']['anthropic-version'] == '2023-06-01'
    assert 'authorization' not in request['headers']
    assert request['body']['model'] == 'claude-test'
    assert request['body']['max_tokens'] == 4096
    assert 'system' in request['body']
    assert request['body']['messages'] == [{'role': 'user', 'content': [{'type': 'text', 'text': 'Perform .'}]}]
    assert 'stream_options' not in request['body'] and 'tools' not in request['body']


def test_native_multitool_preserves_blocks_results_and_grants(native_server, tmp_path):
    url, requests, responses = native_server
    (tmp_path / 'input').write_text('verified evidence')
    first = message('checking', [tool(path='input'), tool('write_file', 't2', path='bad', content='oops')])
    first['content'][0]['citations'] = []
    responses.extend([response(first), response()])
    events = []
    result = native_runtime(url).run(artifact('write'), cwd=tmp_path,
        policy=RunPolicy(allowed_tools=frozenset({'read_file'}), read_roots=('input',)), on_event=events.append)
    assert result.exit_code == 0 and result.token_usage == 14
    assert [t['name'] for t in requests[0]['body']['tools']] == ['read_file']
    continuation = requests[1]['body']['messages']
    assert continuation[1] == {'role': 'assistant', 'content': first['content']}
    results = continuation[2]['content']
    assert [t['tool_use_id'] for t in results] == ['t1', 't2']
    assert 'verified evidence' in results[0]['content']
    assert results[1]['is_error'] is True and 'Tool not granted' in results[1]['content']
    assert not (tmp_path / 'bad').exists()
    receipt = next(e for e in events if e['event'] == 'tool_completed' and e['name'] == 'read_file')
    assert receipt['read_receipts'][0]['path'] == 'input'


def test_native_acquisition_discloses_final_only_after_read(native_server, tmp_path):
    url, requests, responses = native_server
    (tmp_path / 'input').write_text('verified')
    responses.extend([response(text='', calls=[tool(path='input')]), response()])
    result = native_runtime(url).run(replace(artifact('read'), body='FINAL PRIVATE {{args}}'), 'argument',
        acquisition=replace(artifact('read'), body='Read input'), cwd=tmp_path,
        policy=RunPolicy(allowed_tools=frozenset({'read_file'}), read_roots=('input',), initial_tool='read_file'))
    assert result.exit_code == 0
    assert 'FINAL PRIVATE' not in json.dumps(requests[0]['body'])
    assert requests[0]['body']['tool_choice'] == {'type': 'tool', 'name': 'read_file', 'disable_parallel_tool_use': True}
    assert 'FINAL PRIVATE argument' in requests[1]['body']['messages'][2]['content'][-1]['text']
    assert requests[1]['body']['tool_choice'] == {'type': 'auto'}


def test_native_initial_tool_mismatch_executes_nothing(native_server, tmp_path):
    url, requests, responses = native_server
    (tmp_path / 'input').write_text('evidence')
    responses.append(response(text='', calls=[tool(path='input'), tool(call_id='t2', path='input')]))
    events = []
    result = native_runtime(url).run(artifact('read'), cwd=tmp_path,
        policy=RunPolicy(allowed_tools=frozenset({'read_file'}), read_roots=('input',), initial_tool='read_file'),
        on_event=events.append)
    assert result.metadata['provider_error_code'] == 'tool_choice_not_honored'
    assert not any(e['event'] == 'tool_started' for e in events)
    assert len(requests) == 1


def test_native_custom_tool_schema_and_execution(native_server, tmp_path):
    url, requests, responses = native_server
    seen = []
    schema = {'type': 'object', 'properties': {'query': {'type': 'string'}},
              'required': ['query'], 'additionalProperties': False}
    registered = CustomTool('lookup', 'Lookup evidence', schema, lambda a: seen.append(a) or {'found': True}, 'v1')
    responses.extend([response(text='', calls=[tool('lookup', query='a')]), response()])
    result = native_runtime(url, custom_tools={'lookup': registered}, allowed=frozenset({'lookup'})).run(
        artifact(['lookup']), cwd=tmp_path, policy=RunPolicy(allowed_tools=frozenset({'lookup'})))
    assert result.exit_code == 0 and seen == [{'query': 'a'}]
    assert requests[0]['body']['tools'][0]['input_schema'] == schema
    assert '"found": true' in requests[1]['body']['messages'][2]['content'][0]['content']


@pytest.mark.parametrize('stop', ['max_tokens', 'refusal', 'pause_turn', 'unknown'])
def test_native_unsuccessful_stop_never_executes_tools(native_server, stop):
    url, requests, responses = native_server
    responses.append(response(text='', calls=[tool(path='input')], stop=stop))
    events = []
    result = native_runtime(url).run(artifact('read'), policy=RunPolicy(allowed_tools=frozenset({'read_file'})),
                                     on_event=events.append)
    assert result.exit_code != 0 and result.metadata['raw_response_metadata']['stop_reason'] == stop
    assert not any(e['event'] == 'tool_started' for e in events)


@pytest.mark.parametrize('mutation', ['duplicate_id', 'nonobject', 'thinking', 'missing_stop', 'wrong_role'])
def test_native_invalid_response_fails_before_tool_execution(native_server, mutation):
    url, requests, responses = native_server
    value = message('', [tool(path='input')])
    if mutation == 'duplicate_id':
        value['content'].append(tool(path='input'))
    elif mutation == 'nonobject':
        value['content'][0]['input'] = []
    elif mutation == 'thinking':
        value['content'].insert(0, {'type': 'thinking', 'thinking': 'secret', 'signature': 'sig'})
    elif mutation == 'missing_stop':
        value['stop_reason'] = None
    else:
        value['role'] = 'user'
    responses.append(response(value))
    events = []
    result = native_runtime(url).run(artifact('read'), policy=RunPolicy(allowed_tools=frozenset({'read_file'})),
                                     on_event=events.append)
    assert result.exit_code != 0 and len(requests) == 1
    assert not any(e['event'] == 'tool_started' for e in events)


@pytest.mark.parametrize('feature', ['json_mode', 'reasoning_effort', 'effort', 'thinking', 'stream_options'])
def test_native_unsupported_features_fail_before_inference(native_server, feature):
    url, requests, responses = native_server
    with pytest.raises(ValueError, match='unsupported'):
        native_runtime(url, features={feature: True}).run(artifact())
    assert requests == []


def test_native_stop_sequence_is_complete(native_server):
    url, requests, responses = native_server
    responses.append(response(stop='stop_sequence'))
    assert native_runtime(url).run(artifact()).exit_code == 0


def test_mixed_provider_profiles_select_native_or_openai(native_server):
    url, requests, responses = native_server
    old_type, old_body = completion('openai')
    responses.extend([(200, old_type, old_body, {}), response(text='anthropic')])
    config = RuntimeConfig({'old': EndpointConfig(url, 'old', features={'streaming': False}),
                            'new': EndpointConfig(url, 'new', provider='anthropic', features={'streaming': False})},
                           {'fast': 'old', 'strong': 'new'}, 'old')
    rt = ProsaicRuntime(config)
    assert rt.run(artifact()).stdout == 'openai'
    assert rt.run(artifact(model_tier='strong')).stdout == 'anthropic'
    assert [r['path'] for r in requests] == ['/v1/chat/completions', '/v1/messages']


def test_anthropic_can_omit_temperature(native_server):
    url, requests, responses = native_server
    endpoint = EndpointConfig(url, 'claude-test', provider='anthropic', temperature=None,
                              features={'streaming': False})
    responses.append(response())
    result = ProsaicRuntime(RuntimeConfig({'native': endpoint}, {}, 'native')).run(artifact(model_tier=None))
    assert result.exit_code == 0 and 'temperature' not in requests[0]['body']


def test_doctor_uses_native_discovery_auth(native_server, monkeypatch):
    from prosaic_runtime.diagnostics import doctor
    url, requests, responses = native_server
    responses.append(response({'data': [{'id': 'claude-test'}], 'has_more': False}))
    monkeypatch.setenv('TEST_ANTHROPIC_KEY', 'fixture-key')
    report = doctor(native_runtime(url).config, 'native')
    assert report['checks']['discovery']['model_found'] is True
    assert requests[0]['path'] == '/v1/models'
    assert requests[0]['headers'].get('x-api-key') == 'fixture-key'
    assert requests[0]['headers'].get('anthropic-version') == '2023-06-01'
    assert 'authorization' not in requests[0]['headers']
