"""Optional observation cannot affect accepted work or expose private payloads."""
import json
import io
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError

import pytest
import prosaic_runtime as api
from test_runtime import artifact, completion, runtime, server
from test_acquisition import reply, read


def scope(*args, **kwargs):
    cls = getattr(api, 'InvocationScope', None)
    assert cls is not None, 'independent invocation scope is required'
    return cls(*args, **kwargs)


def emitter(observer, **kwargs):
    cls = getattr(api, 'ObserverEmitter', None)
    assert cls is not None, 'bounded shared observer delivery is required'
    return cls(observer, **kwargs)


def test_observer_failure_preserves_result(server):
    url, requests, responses = server
    responses.append(completion('PRIVATE_OUTPUT'))
    records = []
    def observer(record):
        records.append(dict(record))
        record.clear()
        raise RuntimeError('PRIVATE_OBSERVER_MESSAGE')
    result = runtime(api.EndpointConfig(url, 'test', features={'streaming': False})).run(
        artifact(), observer=observer,
        operation_context=scope('attempt-1', run_id='run-1'))
    assert result.exit_code == 0 and result.stdout == 'PRIVATE_OUTPUT'
    assert len(requests) == 1
    assert [r['event'] for r in records] == [
        'invocation_started', 'provider_request_started',
        'provider_request_completed', 'invocation_completed']
    assert [r['sequence'] for r in records] == [1, 2, 3, 4]
    assert all(r['invocation_id'] == 'attempt-1' and r['run_id'] == 'run-1' for r in records)
    assert 'PRIVATE' not in json.dumps(records)
    assert 'accounting_v1' not in result.metadata


def test_terminal_after_cancel():
    records = []
    result = runtime().run(artifact(), cancelled=lambda: True, observer=records.append)
    assert result.exit_code == 130
    assert [r['event'] for r in records] == ['invocation_started', 'invocation_completed']
    assert records[-1]['outcome'] == 'cancelled'


def test_legacy_critical_callback_still_propagates(server):
    url, requests, responses = server
    responses.append(completion('done'))
    def critical(event):
        if event['event'] == 'completed':
            raise RuntimeError('PRIVATE_CRITICAL')
    records = []
    with pytest.raises(RuntimeError, match='PRIVATE_CRITICAL'):
        runtime(api.EndpointConfig(url, 'test', features={'streaming': False})).run(
            artifact(), on_event=critical, observer=records.append)
    assert len(requests) == 1
    assert records[-1]['outcome'] == 'critical_hook_error'
    assert 'PRIVATE' not in json.dumps(records)


def test_admission_failure_emits_once_and_context_is_reset():
    records = []
    with pytest.raises(ValueError):
        runtime().run(artifact(model_tier='missing'), observer=records.append)
    assert records[-1]['outcome'] == 'admission_failure'
    assert [r['event'] for r in records].count('invocation_completed') == 1
    from prosaic_runtime.events import emit
    emit('tool_started', name='read_file')
    assert len(records) == 2


def test_timeout_has_one_terminal_record():
    records = []
    result = runtime().run(artifact(), policy=api.RunPolicy(timeout_s=1e-12), observer=records.append)
    assert result.timed_out
    assert records[-1]['outcome'] == 'timed_out'
    assert [r['event'] for r in records].count('invocation_completed') == 1


def test_observer_process_control_exception_is_visible_and_context_resets():
    def observer(record):
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        runtime().run(artifact(), observer=observer)
    records = []
    runtime().run(artifact(), cancelled=lambda: True, observer=records.append)
    assert [r['sequence'] for r in records] == [1, 2]


def test_concurrent_invocations_are_independently_sequenced():
    def invoke(index):
        records = []
        result = runtime().run(artifact(), observer=records.append, cancelled=lambda: True,
                               operation_context=scope(f'attempt-{index}'))
        return result, records
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(invoke, range(8)))
    for index, (result, records) in enumerate(results):
        assert result.exit_code == 130
        assert [r['sequence'] for r in records] == [1, 2]
        assert {r['invocation_id'] for r in records} == {f'attempt-{index}'}


@pytest.mark.parametrize('value', ['', 'x' * 129, 'é' * 65, 'private\ntext', '\u200d', '\ud800', 7])
def test_invalid_scope_is_rejected(value):
    with pytest.raises(ValueError):
        scope(value)


def test_scope_is_immutable():
    value = scope('attempt')
    with pytest.raises(FrozenInstanceError):
        value.invocation_id = 'changed'


@pytest.mark.parametrize('labels', [{str(i): 'v' for i in range(17)}, {'k' * 65: 'v'},
                                    {'k': 'v' * 129}, {'k': '\n'}, {'k': object()}])
def test_invalid_public_labels_are_rejected_before_delivery(labels):
    with pytest.raises(ValueError):
        emitter(lambda record: pytest.fail('invalid labels dispatched'), source='runtime',
                scope=scope('attempt'), labels=labels)


@pytest.mark.parametrize('revision', ['r' * 129, 'é' * 256])
def test_supported_store_revision_survives_label_trimming(revision):
    records = []
    delivery = emitter(records.append, source='harness', scope=scope('attempt'),
        labels={str(i) + 'k' * 60: 'é' * 64 for i in range(16)})
    delivery.emit('transition_committed', outcome='running', revision=revision, calls=1)
    assert records[0]['revision'] == revision
    assert 'labels' not in records[0]
    assert len(json.dumps(records[0]).encode('utf-8')) <= 4096


@pytest.mark.parametrize('revision', ['r' * 513, 'é' * 257, 'unsafe\nrevision', 'unsafe\u200brevision', 1.5, True])
def test_invalid_store_revision_is_omitted(revision):
    records = []
    emitter(records.append, source='harness', scope=scope('attempt')).emit(
        'transition_committed', outcome='running', revision=revision)
    assert 'revision' not in records[0]


def test_harness_commit_state_survives_record_bounding():
    records = []
    delivery = emitter(records.append, source='harness', scope=scope('é' * 64,
        run_id='r' * 128, step_id='s' * 128, operation_namespace='n' * 128),
        labels={str(i) + 'k' * 60: 'é' * 64 for i in range(16)})
    delivery.emit('blocked_committed', outcome='blocked', revision='opaque-revision', calls=1,
                  text='PRIVATE', path='/PRIVATE', canary='PRIVATE', duration_ms=float('nan'))
    assert len(records) == 1
    record = records[0]
    assert record['outcome'] == 'blocked' and record['revision'] == 'opaque-revision'
    assert record['calls'] == 1
    assert len(json.dumps(record, ensure_ascii=False).encode()) <= 4096
    assert 'PRIVATE' not in json.dumps(record) and 'duration_ms' not in record


def test_harness_unknown_reason_marker_is_explicit_and_private_reasons_are_dropped():
    records = []
    delivery = emitter(records.append, source='harness', scope=scope('attempt'))
    delivery.emit('blocked_committed', outcome='blocked', revision=1, calls=0, reason='unknown')
    delivery.emit('blocked_committed', outcome='blocked', revision=2, calls=0, reason='PRIVATE_REASON')
    assert records[0]['reason'] == 'unknown'
    assert 'reason' not in records[1]


def test_shared_helper_drops_unknown_events_and_invalid_dynamic_fields():
    records = []
    delivery = emitter(records.append, source='runtime', scope=scope('attempt'))
    delivery.emit('text_delta', text='PRIVATE')
    delivery.emit('tool_started', name='PRIVATE_TOOL', turn=-1, call_id='PRIVATE', tool_version='PRIVATE')
    delivery.emit('tool_completed', name='read_file', status='PRIVATE', duration_ms=-1, sha256='PRIVATE')
    assert [r['sequence'] for r in records] == [1, 2]
    assert records[0]['name'] == 'unknown'
    assert records[1]['name'] == 'read_file'
    assert 'PRIVATE' not in json.dumps(records)
    assert 'turn' not in records[0] and 'duration_ms' not in records[1]


def test_provider_connection_failure_is_observed():
    records = []
    result = runtime().run(artifact(), observer=records.append)
    assert result.exit_code != 0
    assert records[-2]['event'] == 'provider_request_completed'
    assert records[-2]['outcome'] == 'execution_failure'
    assert records[-1]['outcome'] == 'execution_failure'


@pytest.mark.parametrize('streaming', [False, True])
def test_observed_tools_preserve_critical_evidence_without_private_previews(server, tmp_path, streaming):
    url, requests, responses = server
    (tmp_path / 'PRIVATE_PATH').write_text('PRIVATE_FILE_CONTENT')
    responses.extend([reply('PRIVATE_PREVIEW', read('PRIVATE_PATH'), streaming),
                      reply('PRIVATE_FINAL', streaming=streaming)])
    records, critical = [], []
    result = runtime(api.EndpointConfig(url, 'test', features={'streaming': streaming})).run(
        artifact('read'), cwd=tmp_path, observer=records.append, on_event=critical.append,
        policy=api.RunPolicy(allowed_tools=frozenset({'read_file'}), read_roots=('.',)))
    assert result.exit_code == 0 and result.stdout == 'PRIVATE_FINAL'
    assert len(requests) == 2
    tool_records = [r for r in records if r['event'].startswith('tool_')]
    assert [r['event'] for r in tool_records] == ['tool_started', 'tool_completed']
    assert tool_records[-1]['status'] == 'ok'
    receipt = next(e for e in critical if e['event'] == 'tool_completed')['read_receipts'][0]
    assert receipt['path'] == 'PRIVATE_PATH'
    assert 'PRIVATE' not in json.dumps(records)
    assert [r['sequence'] for r in records] == list(range(1, len(records) + 1))


def test_runtime_registered_tool_name_remains_observable(server):
    url, requests, responses = server
    seen = []
    tool = api.CustomTool('lookup', 'Lookup', {'type': 'object', 'additionalProperties': False},
                          lambda args: seen.append(args) or {'answer': 1}, 'v1')
    endpoint = api.EndpointConfig(url, 'test', features={'streaming': False})
    config = api.RuntimeConfig({'small': endpoint}, {'fast': 'small'}, 'small', frozenset({'lookup'}))
    responses.extend([completion('', [{'id': 'call', 'type': 'function', 'function': {
        'name': 'lookup', 'arguments': '{}'}}]), completion('done')])
    records = []
    result = api.ProsaicRuntime(config, custom_tools={'lookup': tool}).run(
        artifact(['lookup']), observer=records.append,
        policy=api.RunPolicy(allowed_tools=frozenset({'lookup'})))
    assert result.exit_code == 0 and seen == [{}]
    assert [r['name'] for r in records if r['event'].startswith('tool_')] == ['lookup', 'lookup']


def test_supplied_scope_does_not_resolve_accounting_defaults(server):
    url, requests, responses = server
    responses.append(completion())
    result = runtime(api.EndpointConfig(url, 'test', features={'streaming': False})).run(
        artifact(), operation_context=scope('attempt'))
    assert result.exit_code == 0 and 'accounting_v1' not in result.metadata


def test_invalid_operation_context_rejected_before_critical_callback():
    with pytest.raises(ValueError):
        runtime().run(artifact(), operation_context={'invocation_id': 'attempt'},
                      on_event=lambda event: pytest.fail('invalid scope executed callback'))


@pytest.mark.parametrize('tools', [False, True], ids=['text', 'tool-loop'])
@pytest.mark.parametrize('timeout', ['socket', 'deadline'])
def test_stream_timeout_result_is_observed_as_provider_timeout(monkeypatch, tools, timeout):
    # The real stream reader converts these failures to Result, hiding the
    # transport exception from the HTTP context. Only network I/O and time are fake.
    import time
    clock = [0.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    class Response(io.BytesIO):
        status = 200
        headers = {'Content-Type': 'text/event-stream'}
        def getcode(self):
            return self.status
        def __enter__(self):
            if timeout == 'deadline':
                clock[0] = 11.0
            return super().__enter__()
        def readline(self, *args):
            if timeout == 'deadline':
                pytest.fail('expired stream deadline must precede read')
            raise TimeoutError('PRIVATE_SOCKET_TIMEOUT')
    class Opener:
        def open(self, *args, **kwargs):
            return Response()
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: Opener())
    records, critical = [], []
    result = runtime(api.EndpointConfig('http://unused.invalid/v1', 'test',
        features={'streaming': True})).run(artifact('read' if tools else ''),
            policy=api.RunPolicy(timeout_s=10, allowed_tools=frozenset({'read_file'}) if tools else frozenset()),
            observer=records.append, on_event=critical.append)
    assert result.exit_code == -1 and result.timed_out and result.stdout == ''
    assert result.metadata['provider_error_code'] == 'timeout'
    assert [r['event'] for r in records] == [
        'invocation_started', 'provider_request_started',
        'provider_request_completed', 'invocation_completed']
    assert records[-2]['outcome'] == 'timed_out'
    assert records[-1]['outcome'] == 'timed_out'
    assert critical[-1]['event'] == 'completed'
    assert critical[-1]['exit_code'] == -1
    assert 'PRIVATE' not in json.dumps(records)
