import hashlib
import json
import pytest
from prosaic_runtime import EndpointConfig, RunPolicy
from test_runtime import server, completion, artifact, runtime


def test_successful_read_event_binds_exact_bytes_and_range(server, tmp_path):
    url, _, responses = server
    (tmp_path / 'input').write_bytes(b'first\nsecond\n')
    responses.extend([completion('', [{'id': 'r', 'type': 'function', 'function': {
        'name': 'read_file', 'arguments': '{"path":"input","offset":1,"limit":1}'}}]), completion()])
    events = []
    result = runtime(EndpointConfig(url, 'test', features={'streaming': False})).run(
        artifact('read'), cwd=tmp_path, on_event=events.append,
        policy=RunPolicy(allowed_tools=frozenset({'read_file'}), read_roots=('.',)))
    assert result.exit_code == 0
    event = next(e for e in events if e['event'] == 'tool_completed')
    assert event['read_receipts'] == [{'path': 'input', 'sha256': hashlib.sha256(b'first\nsecond\n').hexdigest(),
                                      'offset': 1, 'lines_read': 1, 'line_count': 2}]
    assert 'content' not in event and 'arguments' not in event


def test_explicit_initial_tool_is_forced_only_on_first_turn(server, tmp_path):
    url, requests, responses = server
    (tmp_path / 'input').write_text('evidence')
    responses.extend([completion('', [{'id': 'r', 'type': 'function', 'function': {
        'name': 'read_file', 'arguments': '{"path":"input"}'}}]), completion()])
    result = runtime(EndpointConfig(url, 'test', features={'streaming': False})).run(
        artifact('read'), cwd=tmp_path,
        policy=RunPolicy(allowed_tools=frozenset({'read_file'}), read_roots=('.',), initial_tool='read_file'))
    assert result.exit_code == 0
    assert requests[0]['tool_choice'] == {'type': 'function', 'function': {'name': 'read_file'}}
    assert requests[1]['tool_choice'] == 'auto'


def test_initial_tool_cannot_elevate_permissions():
    with pytest.raises(ValueError, match='initial_tool'):
        runtime().run(artifact(), policy=RunPolicy(initial_tool='write_file'))


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('names', [[], ['write_file'], ['read_file', 'write_file'], ['read_file', 'read_file']])
def test_endpoint_cannot_skip_or_substitute_explicit_initial_tool(server, tmp_path, streaming, names):
    url, requests, responses = server
    calls = [{'id': f'call-{index}', 'type': 'function', 'function': {
        'name': name, 'arguments': '{"path":"output","content":"must not be written"}'}}
        for index, name in enumerate(names)]
    if streaming:
        parts = [{'choices': [{'delta': {'content': 'Unsupported answer',
                  'tool_calls': [dict(call, index=index) for index, call in enumerate(calls)]},
                  'finish_reason': 'tool_calls' if calls else 'stop'}]},
                 {'choices': [], 'usage': {'total_tokens': 7}}]
        responses.append(('text/event-stream', (''.join('data: ' + json.dumps(p) + '\n\n' for p in parts)
                                                + 'data: [DONE]\n\n').encode()))
    else:
        responses.append(completion('Unsupported answer', calls))
    # If the runtime incorrectly accepts another tool, it must not execute it.
    responses.append(completion())
    events = []
    result = runtime(EndpointConfig(url, 'test', features={'streaming': streaming})).run(
        artifact('write'), cwd=tmp_path, on_event=events.append,
        policy=RunPolicy(allowed_tools=frozenset({'read_file', 'write_file'}),
                         read_roots=('.',), write_paths=('output',), initial_tool='read_file'))
    assert result.exit_code == 1
    assert result.stdout == ''
    assert result.metadata['failure_reason'] == 'tool_choice_not_honored'
    assert result.metadata['expected_tool'] == 'read_file'
    assert result.token_usage == 7
    assert len(requests) == 1  # No automatic retry or further model request.
    assert not (tmp_path / 'output').exists()
    assert not any(e['event'] == 'tool_started' for e in events)


def test_optional_tools_still_allow_a_text_only_completion(server, tmp_path):
    url, requests, responses = server
    responses.append(completion('No read was needed'))
    result = runtime(EndpointConfig(url, 'test', features={'streaming': False})).run(
        artifact('read'), cwd=tmp_path,
        policy=RunPolicy(allowed_tools=frozenset({'read_file'}), read_roots=('.',)))
    assert result.exit_code == 0 and result.stdout == 'No read was needed'
    assert len(requests) == 1 and requests[0]['tool_choice'] == 'auto'
