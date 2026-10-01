from dataclasses import replace
import json
import time
import pytest
from prosaic_runtime import CustomTool, EndpointConfig, ProsaicRuntime, RunPolicy, RuntimeConfig
from test_runtime import server, artifact, completion
from test_acquisition import reply
from test_custom_tools import SCHEMA


def call(raw='{"sku":"SKU-001"}', name='lookup_catalog'):
    return {'id': 'lookup-1', 'type': 'function', 'function': {'name': name, 'arguments': raw}}


def setup(url, seen, streaming=False, **kwargs):
    tool = CustomTool('lookup_catalog', 'Lookup', SCHEMA,
        lambda a: seen.append(a) or {'found': True}, 'v1', **kwargs)
    config = RuntimeConfig({'local': EndpointConfig(url, 'test', features={'streaming': streaming})},
                           {'fast': 'local'}, 'local', frozenset({'lookup_catalog'}))
    return config, tool


@pytest.mark.parametrize('streaming', [False, True])
def test_custom_native_transport_and_owned_events(server, tmp_path, streaming):
    url, requests, responses = server
    seen, events = [], []
    config, tool = setup(url, seen, streaming)
    responses.extend([reply('', [call()], streaming), reply('done', streaming=streaming)])
    registry = {'lookup_catalog': tool}
    runtime = ProsaicRuntime(config, custom_tools=registry)
    registry.clear()
    runtime.tool_descriptors['lookup_catalog']['version'] = 'mutated'
    result = runtime.run(artifact(['lookup_catalog']), cwd=tmp_path,
        policy=RunPolicy(allowed_tools=frozenset({'lookup_catalog'})), on_event=events.append)
    assert result.exit_code == 0 and result.token_usage == 14
    assert seen == [{'sku': 'SKU-001'}]
    assert [t['function']['name'] for t in requests[0]['tools']] == ['lookup_catalog']
    message = next(m for m in requests[1]['messages'] if m['role'] == 'tool')
    assert json.loads(message['content']) == {'status': 'ok', 'result': {'found': True}}
    event = next(e for e in events if e['event'] == 'tool_completed')
    assert event['status'] == 'ok' and event['tool_version'] == 'v1' and event['read_receipts'] == []
    assert 'custom_tools_v1' in runtime.capabilities


@pytest.mark.parametrize('missing', ['registration', 'prose', 'config', 'host', 'read_alias'])
def test_every_grant_required(server, tmp_path, missing):
    url, requests, responses = server
    seen = []
    config, tool = setup(url, seen)
    if missing == 'config':
        config = replace(config, allowed_tools=frozenset())
    runtime = ProsaicRuntime(config, custom_tools={} if missing == 'registration' else {'lookup_catalog': tool})
    prose = '' if missing == 'prose' else 'read' if missing == 'read_alias' else ['lookup_catalog']
    policy = RunPolicy(allowed_tools=frozenset() if missing == 'host' else frozenset({'lookup_catalog'}))
    if missing == 'registration':
        with pytest.raises(ValueError, match='unsupported'):
            runtime.run(artifact(prose), cwd=tmp_path, policy=policy)
        assert requests == []
    else:
        responses.extend([completion('', [call()]), completion('done')])
        runtime.run(artifact(prose), cwd=tmp_path, policy=policy)
        assert 'lookup_catalog' not in [t['function']['name'] for t in requests[0].get('tools', [])]
    assert seen == []


def test_mixed_denied_builtin_and_invalid_custom(server, tmp_path):
    url, requests, responses = server
    seen = []
    config, tool = setup(url, seen)
    responses.extend([completion('', [call(), call('{"sku":"bad"}'),
        call('{"path":"escape","content":"bad"}', 'write_file')]), completion('done')])
    result = ProsaicRuntime(config, custom_tools={'lookup_catalog': tool}).run(
        artifact(['lookup_catalog']), cwd=tmp_path, policy=RunPolicy(allowed_tools=frozenset({'lookup_catalog'})))
    assert result.exit_code == 0 and seen == [{'sku': 'SKU-001'}]
    assert not (tmp_path / 'escape').exists()
    messages = [json.loads(m['content']) for m in requests[1]['messages'] if m['role'] == 'tool']
    assert messages[1]['error'] == 'invalid_arguments' and messages[2]['status'] == 'error'


@pytest.mark.parametrize('deny', [False, True])
def test_custom_acquisition_gates_final_prompt(server, tmp_path, deny):
    url, requests, responses = server
    seen = []
    config, tool = setup(url, seen, authorize=lambda a: not deny)
    responses.extend([completion('', [call()]), completion('done')])
    runtime = ProsaicRuntime(config, custom_tools={'lookup_catalog': tool})
    final = replace(artifact(['lookup_catalog']), body='FINAL PRIVATE CONTRACT')
    result = runtime.run(final, acquisition=artifact(['lookup_catalog']), cwd=tmp_path,
        policy=RunPolicy(allowed_tools=frozenset({'lookup_catalog'}), initial_tool='lookup_catalog'))
    assert 'FINAL PRIVATE CONTRACT' not in json.dumps(requests[0])
    assert len(requests) == (1 if deny else 2)
    assert result.exit_code == (1 if deny else 0)
    if deny:
        assert result.metadata['failure_reason'] == 'acquisition_failed' and seen == []


@pytest.mark.parametrize('kind', ['cancel', 'deadline'])
def test_callback_boundary_preserves_usage_and_stops_followup(server, tmp_path, kind, monkeypatch):
    url, requests, responses = server
    seen, stopped = [], []
    config, tool = setup(url, seen)
    clock = time.monotonic
    monkeypatch.setattr(time, 'monotonic', lambda: clock() + (200 if stopped and kind == 'deadline' else 0))
    def handler(args):
        seen.append(args); stopped.append(True)
        return {'found': True}
    tool = CustomTool('lookup_catalog', 'Lookup', SCHEMA, handler, 'v1')
    responses.extend([completion('', [call()]), completion('done')])
    result = ProsaicRuntime(config, custom_tools={'lookup_catalog': tool}).run(
        artifact(['lookup_catalog']), cwd=tmp_path, policy=RunPolicy(allowed_tools=frozenset({'lookup_catalog'})),
        cancelled=lambda: bool(stopped) and kind == 'cancel')
    assert len(requests) == 1 and result.token_usage == 7
    assert result.exit_code == (130 if kind == 'cancel' else 1)
    assert result.timed_out == (kind == 'deadline')


def test_custom_progress_and_limit_diagnostics_do_not_leak_arguments(server, tmp_path, capsys):
    url, requests, responses = server
    seen, events = [], []
    config, tool = setup(url, seen)
    raw = '{"sku":"synthetic-private-key"}'
    responses.extend([completion('', [call(raw)]), completion('', [call(raw)])])
    result = ProsaicRuntime(config, custom_tools={'lookup_catalog': tool}).run(
        artifact(['lookup_catalog']), cwd=tmp_path,
        policy=RunPolicy(allowed_tools=frozenset({'lookup_catalog'}), max_tool_rounds=1), on_event=events.append)
    assert result.exit_code == 1
    captured = capsys.readouterr()
    assert 'synthetic-private-key' not in captured.out + captured.err + result.stderr + json.dumps(events) + json.dumps(result.metadata)


def test_initial_tool_mismatch_and_cross_invocation_grants(server, tmp_path):
    url, requests, responses = server
    seen = []
    config, tool = setup(url, seen)
    runtime = ProsaicRuntime(config, custom_tools={'lookup_catalog': tool})
    with pytest.raises(ValueError, match='initial_tool'):
        runtime.run(artifact(), policy=RunPolicy(allowed_tools=frozenset({'lookup_catalog'}), initial_tool='lookup_catalog'))
    assert requests == []
    responses.extend([completion('', [call()]), completion('done'), completion('', [call()]), completion('done')])
    runtime.run(artifact(['lookup_catalog']), cwd=tmp_path, policy=RunPolicy(allowed_tools=frozenset({'lookup_catalog'})))
    runtime.run(artifact(['lookup_catalog']), cwd=tmp_path)
    assert len(seen) == 1


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('shape', ['object', 'null', 'missing', 'wrong_type', 'missing_type'])
def test_malformed_call_shape_cannot_be_normalized_into_custom_execution(server, tmp_path, streaming, shape):
    url, requests, responses = server
    seen, events = [], []
    config, _ = setup(url, seen, streaming)
    tool = CustomTool('lookup_catalog', 'Optional arguments', {'type': 'object', 'additionalProperties': False},
                      lambda a: seen.append(a) or {'found': True}, 'v1')
    malformed = call('{}')
    if shape == 'object':
        malformed['function']['arguments'] = {'unexpected': 'ignored'}
    elif shape == 'null':
        malformed['function']['arguments'] = None
    elif shape == 'missing':
        malformed['function'].pop('arguments')
    elif shape == 'wrong_type':
        malformed['type'] = 'not-a-function'
    else:
        malformed.pop('type')
    responses.extend([reply('', [malformed], streaming), reply('done', streaming=streaming)])
    result = ProsaicRuntime(config, custom_tools={'lookup_catalog': tool}).run(
        artifact(['lookup_catalog']), cwd=tmp_path,
        policy=RunPolicy(allowed_tools=frozenset({'lookup_catalog'})), on_event=events.append)
    assert seen == []
    assert not any(e['event'] == 'tool_completed' and e['status'] == 'ok' for e in events)


def test_sse_invalid_fragment_cannot_be_dropped_before_valid_arguments(server, tmp_path):
    url, requests, responses = server
    seen = []
    config, _ = setup(url, seen, True)
    tool = CustomTool('lookup_catalog', 'Optional', {'type': 'object', 'additionalProperties': False},
                      lambda a: seen.append(a), 'v1')
    chunks = [{'choices': [{'delta': {'tool_calls': [dict(call(None), index=0)]}, 'finish_reason': None}]},
              {'choices': [{'delta': {'tool_calls': [{'index': 0, 'function': {'arguments': '{}'}}]}, 'finish_reason': 'tool_calls'}]},
              {'choices': [], 'usage': {'total_tokens': 7}}]
    responses.extend([('text/event-stream', (''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks) + 'data: [DONE]\n\n').encode()),
                      reply('done', streaming=True)])
    ProsaicRuntime(config, custom_tools={'lookup_catalog': tool}).run(
        artifact(['lookup_catalog']), cwd=tmp_path, policy=RunPolicy(allowed_tools=frozenset({'lookup_catalog'})))
    assert seen == []
