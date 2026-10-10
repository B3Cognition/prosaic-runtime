"""Closed replay evidence and explicitly bounded, owned fixture execution."""
from copy import deepcopy
from dataclasses import replace
from email.message import Message
import io
import json

import pytest
import prosaic_runtime as api
from prosaic_runtime import EndpointConfig, RuntimeConfig, RunPolicy
from test_artifact_admission import config
from test_runtime import server, completion
from test_anthropic import native_server, message
from test_anthropic_stream import events, sse

CASES = ('text_complete', 'stream_terminal', 'required_tool', 'arguments_validated',
         'strict_json', 'usage_complete', 'cancellation_boundary')
FINGERPRINT = 'a' * 64


def evaluate(observations, suite='text-tools-v1', fingerprint=FINGERPRINT):
    evaluator = getattr(api, 'evaluate_conformance', None)
    assert callable(evaluator), 'public pure conformance evaluator required'
    return evaluator(suite, fingerprint, observations)


def evidence(case='text_complete', **changes):
    return {case: {'state': 'passed', 'reason': 'complete', 'suiteId': 'text-tools-v1',
                   'profileFingerprint': FINGERPRINT, 'origin': 'fixture', **changes}}


def run(config, profile='small', **kwargs):
    import prosaic_runtime.diagnostics as diagnostics
    runner = getattr(diagnostics, 'conformance', None)
    assert callable(runner), 'explicit opt-in conformance runner required'
    return runner(config, profile, **kwargs)


def test_canonical_order_owned_snapshot_and_source_version():
    observations = {**evidence(), **evidence('strict_json')}
    before = deepcopy(observations)
    a = evaluate(observations)
    b = evaluate(dict(reversed(list(observations.items()))))
    encode = lambda value: json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False)
    assert encode(a) == encode(b)
    assert a['version'] == 1 and a['runtimeVersion'] == '0.8.0'
    assert tuple(case['id'] for case in a['cases']) == CASES
    assert a['cases'][0]['origin'] == 'fixture'
    assert a['cases'][1]['state'] == 'not_run'
    assert a['cases'][1]['origin'] == 'unexecuted'
    assert a['qualification'] == 'not_qualified'
    observations['text_complete']['state'] = 'failed'
    assert a['cases'][0]['state'] == 'passed' and before != observations


@pytest.mark.parametrize('origin,want', [('fixture', 'fixture_passed'), ('live', 'qualified')])
def test_only_complete_single_origin_evidence_qualifies(origin, want):
    observations = {}
    for case in CASES:
        reason = ('invalid_arguments' if case == 'arguments_validated' else
                  'cancelled_before_followup' if case == 'cancellation_boundary' else 'complete')
        observations.update(evidence(case, origin=origin, reason=reason))
    assert evaluate(observations)['qualification'] == want
    observations['strict_json']['origin'] = 'fixture' if origin == 'live' else 'live'
    assert evaluate(observations)['qualification'] == 'not_qualified'
    observations['strict_json'].update(state='unsupported', reason='not_requested')
    assert evaluate(observations)['qualification'] == 'not_qualified'


@pytest.mark.parametrize('changes', [
    {'suiteId': 'foreign'}, {'profileFingerprint': 'b' * 64}, {'origin': 'unexecuted'},
    {'origin': 'unknown'}, {'state': 'success'}, {'reason': 'provider-secret'},
    {'state': 'passed', 'reason': 'usage_unknown'}, {'rawText': 'private'},
    {'url': 'http://private'}, {'headers': {}}, {'outputSha256': 'G' * 64},
    {'providerRequests': True}, {'toolCalls': -1}, {'reportedTokens': 2**63},
    {'handlerCalls': float('nan')}, {'optional': True}, {'depth': [[[]]]},
])
def test_replay_rejects_foreign_or_open_evidence(changes):
    with pytest.raises(ValueError):
        evaluate(evidence(**changes))


@pytest.mark.parametrize('observations,suite,fingerprint', [
    ([], 'text-tools-v1', FINGERPRINT), ({'foreign': {}}, 'text-tools-v1', FINGERPRINT),
    ({}, 'other', FINGERPRINT), ({}, [], FINGERPRINT), ({}, 'text-tools-v1', 'bad'),
    ({}, 'text-tools-v1', None),
])
def test_replay_rejects_invalid_envelopes(observations, suite, fingerprint):
    with pytest.raises(ValueError):
        evaluate(observations, suite, fingerprint)


def test_replay_bounds_bytes_depth_cycles_and_large_integers():
    bad = evidence(rawText='x' * 65537)
    cyclic = evidence()
    cyclic['text_complete']['cycle'] = cyclic
    deep = evidence()
    node = deep['text_complete']
    for _ in range(17):
        node['nested'] = {}
        node = node['nested']
    for observations in (bad, cyclic, deep, evidence(handlerCalls=10**10000)):
        with pytest.raises(ValueError):
            evaluate(observations)


def test_no_live_returns_before_constructor_probes_and_callbacks(monkeypatch):
    import urllib.request
    import subprocess
    def forbidden(*args, **kwargs):
        pytest.fail('unexpected effect in default conformance')
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', forbidden)
    monkeypatch.setattr(api.ProsaicRuntime, '__init__', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    report = run(config(), 'openai', observer=forbidden)
    assert report['qualification'] == 'not_qualified'
    assert all(case['state'] == 'not_run' for case in report['cases'])


def test_fingerprint_ignores_credentials_but_binds_execution_settings():
    base = EndpointConfig('http://localhost:9/v1', 'model', features={'streaming': False})
    def report(endpoint):
        return run(RuntimeConfig({'small': endpoint}, {}, 'small'))['profileFingerprint']
    want = report(base)
    assert report(replace(base, api_key_env='SECRET', api_key_file='/missing/private')) == want
    for change in ({'model': 'other'}, {'base_url': 'http://localhost:8/v1'},
                   {'temperature': 0.3}, {'max_tokens': 20}, {'max_response_bytes': 1234},
                   {'features': {'streaming': True}}):
        assert report(replace(base, **change)) != want


def suite_config(url, provider='openai-compatible', **features):
    return RuntimeConfig({'small': EndpointConfig(url, 'fixture-model', provider=provider,
        features=features or {'streaming': True})}, {}, 'small',
        tool_directories=('/missing/must-not-probe',))


def tool_call(value=7, **changes):
    return {'id': 'fixture-call', 'type': 'function', 'function': {
        'name': 'conformance_lookup', 'arguments': json.dumps({'value': value})}, **changes}


def openai_stream(text='done', *, terminal=True):
    chunks = [{'choices': [{'delta': {'content': text}, 'finish_reason': None}]}]
    if terminal:
        chunks += [{'choices': [{'delta': {}, 'finish_reason': 'stop'}]},
                   {'choices': [], 'usage': {'prompt_tokens': 5, 'completion_tokens': 2, 'total_tokens': 7}}]
    body = ''.join('data: ' + json.dumps(chunk) + '\n\n' for chunk in chunks)
    return 'text/event-stream', (body + ('data: [DONE]\n\n' if terminal else '')).encode()


def openai_suite():
    return [completion(), openai_stream(), completion('', [tool_call()]), completion(),
            completion('', [tool_call('invalid')]), completion(), completion('{"value":7}'),
            completion(), completion('', [tool_call()])]


def native_suite():
    def turn(text='done', value=None):
        calls = () if value is None else ({'type': 'tool_use', 'id': 'fixture-call',
                 'name': 'conformance_lookup', 'input': {'value': value}},)
        return sse(events(message(text, calls)))
    return [turn(), turn(), turn('', 7), turn(), turn('', 'invalid'), turn(),
            turn('{"value":7}'), turn(), turn('', 7)]


def case(report, name):
    return next(c for c in report['cases'] if c['id'] == name)


def test_complete_openai_fixture_observes_all_checks_and_never_live_qualifies(server):
    url, requests, responses = server
    responses.extend(openai_suite())
    observed = []
    report = run(suite_config(url, streaming=True, json_mode=True), live=True,
                 evidence_origin='fixture', observer=observed.append)
    assert report['qualification'] == 'fixture_passed'
    assert all(c['state'] == 'passed' and c['origin'] == 'fixture' for c in report['cases'])
    assert len(requests) == 9
    assert requests[2]['tool_choice']['function']['name'] == 'conformance_lookup'
    assert requests[4]['tool_choice']['function']['name'] == 'conformance_lookup'
    assert case(report, 'arguments_validated')['handlerCalls'] == 0
    assert case(report, 'cancellation_boundary')['providerRequests'] == 1
    assert case(report, 'cancellation_boundary')['handlerCalls'] == 1
    assert observed and 'fixture-model' not in json.dumps(report)
    assert url not in json.dumps(report)


def test_complete_native_fixture_observes_all_checks(native_server):
    url, requests, responses = native_server
    responses.extend(native_suite())
    report = run(suite_config(url, 'anthropic'), live=True, evidence_origin='fixture')
    assert report['qualification'] == 'fixture_passed'
    assert len(requests) == 9
    assert requests[2]['body']['tool_choice']['type'] == 'tool'
    assert all(r['path'] == '/v1/messages' for r in requests)


@pytest.mark.parametrize('scenario,reply,want', [
    ('required_tool', completion('I used conformance_lookup'), 'failed'),
    ('strict_json', completion('I returned valid JSON'), 'failed'),
    ('strict_json', completion('{"value":7,"value":7}'), 'failed'),
    ('strict_json', completion('{"value":NaN}'), 'failed'),
    ('strict_json', completion('{"value":true}'), 'failed'),
    ('stream_terminal', completion('data: [DONE] finish_reason=stop'), 'failed'),
    ('stream_terminal', openai_stream(terminal=False), 'failed'),
    ('stream_terminal', ('text/event-stream', openai_stream()[1].replace(b'data: [DONE]\n\n', b'')), 'failed'),
    ('arguments_validated', completion('', [tool_call('invalid', type='invalid')]), 'failed'),
])
def test_requested_flags_prose_and_unrelated_errors_cannot_pass(server, scenario, reply, want):
    url, requests, responses = server
    queue = openai_suite()
    index = {'stream_terminal': 1, 'required_tool': 2, 'arguments_validated': 4, 'strict_json': 6}[scenario]
    queue[index] = reply
    if scenario == 'required_tool':
        queue.pop(3)  # No native call means the enforcement failure cannot follow up.
    responses.extend(queue)
    report = run(suite_config(url, streaming=True, json_mode=True), live=True, evidence_origin='fixture')
    assert case(report, scenario)['state'] == want
    assert report['qualification'] == 'not_qualified'
    if scenario == 'arguments_validated':
        assert case(report, scenario)['handlerCalls'] == 0


def test_unknown_usage_halts_the_shared_suite(server):
    url, requests, responses = server
    responses.append(('application/json', json.dumps({'choices': [{'message': {'content': 'done'},
        'finish_reason': 'stop'}]}).encode()))
    report = run(suite_config(url), live=True, evidence_origin='fixture')
    assert len(requests) == 1
    assert case(report, 'usage_complete')['state'] == 'unknown'
    assert case(report, 'usage_complete')['reason'] == 'usage_unknown'
    assert case(report, 'stream_terminal')['state'] == 'not_run'


def test_shared_request_and_token_allowances_stop_before_next_scenario(server):
    url, requests, responses = server
    responses.extend(openai_suite())
    report = run(suite_config(url), live=True, evidence_origin='fixture',
                 policy=RunPolicy(max_provider_requests=1, max_tool_calls=4, max_reported_tokens=100))
    assert len(requests) == 1
    assert case(report, 'text_complete')['state'] == 'passed'
    assert all(c['state'] == 'not_run' for c in report['cases'][1:])
    responses.clear()
    responses.extend(openai_suite())
    report = run(suite_config(url), live=True, evidence_origin='fixture',
                 policy=RunPolicy(max_provider_requests=12, max_tool_calls=4, max_reported_tokens=7))
    assert len(requests) == 2
    assert all(c['state'] == 'not_run' for c in report['cases'][1:])


def test_unbounded_caller_policy_is_rejected_before_constructor(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('invalid policy must not construct runtime')
    monkeypatch.setattr(api.ProsaicRuntime, '__init__', forbidden)
    with pytest.raises(ValueError):
        run(suite_config('http://localhost:9/v1'), live=True, policy=RunPolicy(), evidence_origin='fixture')


def test_configured_off_checks_remain_unsupported(server):
    url, requests, responses = server
    queue = openai_suite()
    queue.pop(6)
    queue.pop(1)
    responses.extend(queue)
    report = run(suite_config(url, streaming=False, json_mode=False), live=True, evidence_origin='fixture')
    assert case(report, 'stream_terminal')['state'] == 'unsupported'
    assert case(report, 'strict_json')['state'] == 'unsupported'
    assert report['qualification'] == 'not_qualified' and len(requests) == 7


def test_dispatch_evidence_context_resets_after_cancel_and_next_suite(server):
    from prosaic_runtime.conformance import _tool_evidence
    url, requests, responses = server
    responses.extend(openai_suite())
    first = run(suite_config(url), live=True, evidence_origin='fixture')
    assert first['qualification'] == 'fixture_passed'
    assert _tool_evidence.get() is None
    queue = openai_suite()
    queue[4] = completion('', [tool_call('invalid', type='invalid')])
    responses.extend(queue)
    second = run(suite_config(url), live=True, evidence_origin='fixture')
    assert case(second, 'arguments_validated')['state'] == 'failed'
    assert _tool_evidence.get() is None


def test_native_missing_terminal_never_passes(native_server):
    url, requests, responses = native_server
    queue = native_suite()
    queue[1] = sse(events()[:-1])
    responses.extend(queue)
    report = run(suite_config(url, 'anthropic'), live=True, evidence_origin='fixture')
    assert case(report, 'stream_terminal')['state'] == 'failed'
    assert report['qualification'] == 'not_qualified'


def test_shared_deadline_stops_remaining_scenarios(native_server, monkeypatch):
    import time
    from types import SimpleNamespace
    import test_anthropic
    clock = [0.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    # Advance only after the fixture receives the first real request. This
    # tests the shared deadline without requiring cold admission to fit in 40ms.
    monkeypatch.setattr(test_anthropic, 'time', SimpleNamespace(
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds)))
    url, requests, responses = native_server
    responses.append((*native_suite()[0], 6.0))
    report = run(suite_config(url, 'anthropic'), live=True, evidence_origin='fixture',
                 policy=RunPolicy(timeout_s=5.0, max_provider_requests=12, max_tool_calls=4,
                                  max_reported_tokens=32768))
    assert len(requests) == 1
    assert case(report, 'text_complete')['state'] == 'failed'
    assert case(report, 'stream_terminal')['state'] == 'not_run'


def test_suite_clamps_provider_output_and_stops_on_unknown_usage(server):
    url, requests, responses = server
    responses.append(completion('x' * 70000))
    report = run(suite_config(url), live=True, evidence_origin='fixture')
    assert len(requests) == 1
    assert case(report, 'text_complete')['state'] == 'failed'
    assert case(report, 'usage_complete')['state'] == 'unknown'
    assert len(json.dumps(report).encode()) < 65536


def test_private_collector_resets_on_propagating_observer_error(server):
    from prosaic_runtime.conformance import _tool_evidence, _suite_deadline
    url, requests, responses = server
    class HostAbort(BaseException):
        pass
    def observer(event):
        raise HostAbort()
    with pytest.raises(HostAbort):
        run(suite_config(url), live=True, evidence_origin='fixture', observer=observer)
    assert _tool_evidence.get() is None and requests == []
    assert _suite_deadline.get() is None


def test_constructor_time_is_charged_to_absolute_suite_deadline(server, monkeypatch):
    import time
    url, requests, responses = server
    responses.append(completion())
    original = api.ProsaicRuntime.__init__
    def delayed_init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        time.sleep(0.08)
    monkeypatch.setattr(api.ProsaicRuntime, '__init__', delayed_init)
    report = run(suite_config(url), live=True, evidence_origin='fixture',
        policy=RunPolicy(timeout_s=0.04, max_provider_requests=12, max_tool_calls=4,
                         max_reported_tokens=32768))
    assert requests == []
    assert all(c['state'] == 'not_run' for c in report['cases'])


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
@pytest.mark.parametrize('delayed_event', ['invocation_started', 'provider_request_started'])
@pytest.mark.parametrize('observer_raises', [False, True])
def test_absolute_suite_deadline_prevents_dispatch_after_observer(
        monkeypatch, provider, delayed_event, observer_raises):
    """Relative invocation clocks must not authorize work past the suite deadline."""
    from prosaic_runtime.conformance import _tool_evidence, _suite_deadline
    clock, attempts = [100.0], []
    class Response(io.BytesIO):
        status = 200
        headers = Message()
        headers['Content-Type'] = 'application/json'
    body = completion()[1] if provider == 'openai-compatible' else json.dumps(message()).encode()
    def local_open(*args, **kwargs):
        attempts.append(round(clock[0] - 100, 3))
        return Response(body)
    def observer(event):
        if event['event'] == delayed_event:
            clock[0] += 0.08
            if observer_raises:
                raise RuntimeError('private-host-error')
    monkeypatch.setattr('time.monotonic', lambda: clock[0])
    monkeypatch.setattr('urllib.request.OpenerDirector.open', local_open)
    report = run(suite_config('http://localhost:9/v1', provider, streaming=False),
        live=True, evidence_origin='fixture', observer=observer,
        policy=RunPolicy(timeout_s=0.04, max_provider_requests=12, max_tool_calls=4,
                         max_reported_tokens=32768))
    assert attempts == []
    assert case(report, 'text_complete')['state'] == 'failed'
    assert report['qualification'] == 'not_qualified'
    assert 'private-host-error' not in json.dumps(report)
    assert _tool_evidence.get() is None
    assert _suite_deadline.get() is None


def test_absolute_suite_deadline_scope_does_not_change_ordinary_runtime(monkeypatch):
    """Conformance callback guards must not leak into the next ordinary invocation."""
    from prosaic_runtime import ProsaicArtifact, ProsaicRuntime
    from prosaic_runtime.conformance import _tool_evidence
    clock, attempts = [100.0], []
    class Response(io.BytesIO):
        status = 200
        headers = Message()
        headers['Content-Type'] = 'application/json'
    def local_open(*args, **kwargs):
        attempts.append(round(clock[0] - 100, 3))
        return Response(completion()[1])
    def observer(event):
        if event['event'] == 'invocation_started':
            clock[0] += 0.08
    monkeypatch.setattr('time.monotonic', lambda: clock[0])
    monkeypatch.setattr('urllib.request.OpenerDirector.open', local_open)
    config = suite_config('http://localhost:9/v1', streaming=False)
    run(config, live=True, evidence_origin='fixture', observer=observer,
        policy=RunPolicy(timeout_s=0.04, max_provider_requests=12, max_tool_calls=4,
                         max_reported_tokens=32768))
    assert attempts == []
    assert _tool_evidence.get() is None
    result = ProsaicRuntime(replace(config, tool_directories=())).run(
        ProsaicArtifact('ordinary', 'subagent', {}, 'Return done.'),
        observer=observer, policy=RunPolicy(timeout_s=0.04))
    assert attempts == [0.16]
    assert result.exit_code == 0 and result.stdout == 'done'
