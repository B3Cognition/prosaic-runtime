import hashlib
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
    import pytest
    with pytest.raises(ValueError, match='initial_tool'):
        runtime().run(artifact(), policy=RunPolicy(initial_tool='write_file'))
