"""Invocation allowances stop real fixture dispatch and retain incurred evidence."""
import json
import io
from dataclasses import asdict, replace
from email.message import Message
from urllib.error import HTTPError, URLError

import pytest

from prosaic_runtime import CustomTool, EndpointConfig, ProsaicRuntime, RunPolicy
from prosaic_runtime.types import Invocation
from prosaic_runtime.openai_compatible import OpenAICompatibleBackend
from prosaic_runtime.accounting import MemoryRecorder
from test_runtime import artifact, completion, runtime, server
from test_custom_tool_transport import setup, call
from test_acquisition import reply
from test_anthropic import native_server, native_runtime, message, response
from test_anthropic_stream import events, sse


@pytest.fixture
def budget_transport(monkeypatch):
    """Replace only external I/O; real request construction/Capture/parsers run."""
    requests, responses = [], []

    class Response(io.BytesIO):
        status = 200

        def __init__(self, content_type, body, timed_out=False):
            super().__init__(body)
            self.headers = Message()
            self.headers['Content-Type'] = content_type
            self.timed_out = timed_out

        def readline(self, *args):
            if self.timed_out:
                raise TimeoutError('fixture timeout')
            return super().readline(*args)

        def read1(self, *args):
            if self.timed_out:
                raise TimeoutError('fixture timeout')
            return super().read1(*args)

    class Opener:
        def open(self, request, timeout=None):
            requests.append(json.loads(request.data))
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return Response(*item)

    opener = Opener()
    monkeypatch.setattr('urllib.request.build_opener', lambda *handlers: opener)
    monkeypatch.setattr('urllib.request.urlopen', opener.open)
    return requests, responses


@pytest.mark.parametrize('field', ['max_provider_requests', 'max_tool_calls', 'max_reported_tokens'])
@pytest.mark.parametrize('value', [True, -1, 1.5, float('inf'), float('nan'), '1'])
def test_allowances_reject_nonnegative_integer_impostors(field, value):
    with pytest.raises(ValueError):
        RunPolicy(**{field: value})


@pytest.mark.parametrize('field,reason', [('max_provider_requests', 'provider_request_limit'),
                                        ('max_reported_tokens', 'token_limit')])
def test_zero_request_or_token_allowance_prevents_accounting_and_network(server, field, reason):
    url, requests, _ = server
    recorder = MemoryRecorder()
    result = runtime(EndpointConfig(url, 'test'), accounting=recorder).run(
        artifact(), policy=RunPolicy(**{field: 0}))
    assert requests == [] and recorder.intents == {}
    assert result.metadata['failure_reason'] == reason
    assert result.metadata['invocation_budgets_v1']['provider_requests'] == 0


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('with_tools', [False, True])
@pytest.mark.parametrize('usage,cap,reason,want', [
    (None, 7, 'usage_unknown', None), ({}, 7, 'usage_unknown', None),
    ({'prompt_tokens': 2}, 7, 'usage_unknown', None),
    ({'total_tokens': 8}, 7, 'token_limit', 8),
    ({'total_tokens': 7}, 7, None, 7),
    ({'total_tokens': True}, 7, 'usage_unknown', None),
    ({'total_tokens': 7.0}, 7, 'usage_unknown', None),
    ({'total_tokens': -1}, 7, 'usage_unknown', None),
    ({'total_tokens': 7, 'prompt_tokens': 1.5}, 7, 'usage_unknown', None),
    ({'prompt_tokens': 2, 'completion_tokens': 3}, 5, None, 5),
    ({'total_tokens': 7, 'prompt_tokens': 2, 'completion_tokens': 3}, 7, 'usage_unknown', None),
])
def test_openai_terminal_usage_is_enforced_once(server, streaming, with_tools, usage, cap, reason, want):
    url, requests, responses = server
    if streaming:
        chunks = [{'choices': [{'delta': {'content': 'done'}, 'finish_reason': 'stop'}]},
                  {'choices': [], **({'usage': usage} if usage is not None else {})}]
        responses.append(('text/event-stream', (''.join('data: ' + json.dumps(c) + '\n\n'
            for c in chunks) + 'data: [DONE]\n\n').encode()))
    else:
        kind, body = completion('done')
        value = json.loads(body)
        value.pop('usage')
        if usage is not None:
            value['usage'] = usage
        responses.append((kind, json.dumps(value).encode()))
    result = runtime(EndpointConfig(url, 'test', features={'streaming': streaming})).run(
        artifact('read' if with_tools else None), policy=RunPolicy(max_reported_tokens=cap,
            allowed_tools=frozenset({'read_file'}) if with_tools else frozenset(), read_roots=('.',)))
    assert len(requests) == 1
    assert result.exit_code == (0 if reason is None else 1)
    assert result.stdout == 'done'
    assert result.token_usage == want
    assert result.metadata.get('failure_reason') == reason
    counts = result.metadata['invocation_budgets_v1']
    assert counts['provider_requests'] == 1 and counts['tool_calls'] == 0
    if want is not None:
        assert counts['reported_tokens'] == want


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('usage,reason,want', [
    ({'input_tokens': 5, 'output_tokens': 2}, None, 7),
    ({'input_tokens': 5, 'output_tokens': 3}, 'token_limit', 8),
    ({'input_tokens': 5}, 'usage_unknown', None),
    ({'input_tokens': True, 'output_tokens': 2}, 'usage_unknown', None),
    ({'input_tokens': 5, 'output_tokens': -1}, 'usage_unknown', None),
    ({'input_tokens': 1, 'output_tokens': 2, 'cache_read_input_tokens': 4}, None, 7),
])
def test_native_terminal_usage_is_enforced_once(native_server, streaming, usage, reason, want):
    url, requests, responses = native_server
    value = message(usage=usage)
    if streaming:
        items = events(message())
        # Preserve missing/invalid terminal output evidence instead of fixture defaults.
        items[0]['message']['usage'] = dict(usage)
        items[-2]['usage'] = {k: v for k, v in usage.items() if k == 'output_tokens'}
        responses.append(sse(items))
    else:
        responses.append(response(value))
    result = native_runtime(url, features={'streaming': streaming}).run(
        artifact(), policy=RunPolicy(max_reported_tokens=7))
    assert len(requests) == 1
    assert result.exit_code == (0 if reason is None else 1)
    assert result.token_usage == want
    assert result.metadata.get('failure_reason') == reason
    assert result.metadata['invocation_budgets_v1']['provider_requests'] == 1


@pytest.mark.parametrize('first', [call(), call('{"sku":"bad"}'), call(name='write_file'),
                                 {'id': 'bad', 'function': {} }])
@pytest.mark.parametrize('streaming', [False, True])
def test_multicall_batch_consumes_one_allowance_per_attempt(server, first, streaming):
    url, requests, responses = server
    seen = []
    config, tool = setup(url, seen, streaming)
    responses.append(reply('', [first, call(), call()], streaming))
    result = ProsaicRuntime(config, custom_tools={'lookup_catalog': tool}).run(
        artifact(['lookup_catalog']), policy=RunPolicy(
            allowed_tools=frozenset({'lookup_catalog'}), max_tool_calls=1))
    assert len(seen) == (1 if first == call() else 0)
    assert len(requests) == 1 and result.token_usage == 7
    assert result.metadata['failure_reason'] == 'tool_call_limit'
    assert result.metadata['invocation_budgets_v1']['tool_calls'] == 1


@pytest.mark.parametrize('cap,reason', [(0, 'tool_call_limit'), (1, 'provider_request_limit')])
def test_acquisition_and_followup_share_allowances(server, cap, reason):
    url, requests, responses = server
    seen = []
    config, tool = setup(url, seen)
    responses.append(completion('', [call()]))
    result = ProsaicRuntime(config, custom_tools={'lookup_catalog': tool}).run(
        artifact(['lookup_catalog']), acquisition=artifact(['lookup_catalog']),
        policy=RunPolicy(allowed_tools=frozenset({'lookup_catalog'}), initial_tool='lookup_catalog',
                         max_provider_requests=1, max_tool_calls=cap))
    assert len(requests) == 1 and len(seen) == cap
    assert result.token_usage == 7 and result.metadata['failure_reason'] == reason


@pytest.mark.parametrize('cap,usage,reason', [(7, {'total_tokens': 7}, 'token_limit'),
                                         (20, None, 'usage_unknown')])
def test_token_exhaustion_stops_tools_before_dispatch(server, cap, usage, reason):
    url, requests, responses = server
    seen = []
    config, tool = setup(url, seen)
    kind, body = completion('', [call()])
    value = json.loads(body)
    value.pop('usage')
    if usage is not None:
        value['usage'] = usage
    responses.append((kind, json.dumps(value).encode()))
    result = ProsaicRuntime(config, custom_tools={'lookup_catalog': tool}).run(
        artifact(['lookup_catalog']), policy=RunPolicy(
            allowed_tools=frozenset({'lookup_catalog'}), max_reported_tokens=cap))
    assert seen == [] and len(requests) == 1
    assert result.metadata['failure_reason'] == reason
    assert result.token_usage == (7 if usage else None)


def test_unknown_later_turn_retains_known_sum_and_accounting(server):
    url, requests, responses = server
    seen = []
    config, tool = setup(url, seen)
    kind, body = completion('done')
    value = json.loads(body)
    value.pop('usage')
    responses.extend([completion('', [call()]), (kind, json.dumps(value).encode())])
    recorder = MemoryRecorder()
    result = ProsaicRuntime(config, custom_tools={'lookup_catalog': tool}, accounting=recorder).run(
        artifact(['lookup_catalog']), policy=RunPolicy(
            allowed_tools=frozenset({'lookup_catalog'}), max_reported_tokens=20))
    assert len(requests) == len(recorder.observations) == 2
    assert result.token_usage is None and result.metadata['reported_token_usage'] == 7
    assert result.metadata['failure_reason'] == 'usage_unknown'


def test_overcap_paid_terminal_response_is_recorded_before_failure(server):
    url, requests, responses = server
    responses.append(completion('done'))
    recorder = MemoryRecorder()
    result = runtime(EndpointConfig(url, 'test', features={'streaming': False}), accounting=recorder).run(
        artifact(), policy=RunPolicy(max_reported_tokens=6))
    assert len(requests) == len(recorder.observations) == 1
    observation = next(iter(recorder.observations.values()))
    assert observation['outcome'] == 'completed' and observation['usage']['total_tokens'] == 7
    assert result.token_usage == 7 and result.metadata['failure_reason'] == 'token_limit'


def test_zero_tool_allowance_permits_text_and_disabled_caps_preserve_metadata(server):
    url, requests, responses = server
    responses.extend([completion('done'), completion('done')])
    runner = runtime(EndpointConfig(url, 'test', features={'streaming': False}))
    old = runner.run(artifact())
    bounded = runner.run(artifact(), policy=RunPolicy(max_tool_calls=0))
    counts = bounded.metadata.pop('invocation_budgets_v1')
    assert counts == {'provider_requests': 1, 'tool_calls': 0, 'reported_tokens': 7, 'usage_complete': True}
    assert bounded == old and len(requests) == 2
    assert 'invocation_budgets_v1' in runner.capabilities


@pytest.mark.parametrize('failure', ['http', 'prepare', 'observe'])
def test_failed_request_attempts_are_counted_without_automatic_retry(native_server, failure):
    url, requests, responses = native_server
    class Recorder(MemoryRecorder):
        def prepare(self, intent):
            if failure == 'prepare':
                raise RuntimeError('fixture failure')
            super().prepare(intent)

        def observe(self, call_id, observation):
            if failure == 'observe':
                raise RuntimeError('fixture failure')
            super().observe(call_id, observation)
    recorder = Recorder(provider_id='anthropic')
    responses.append((503, 'application/json', b'{"error": {"type": "overloaded_error"}}', {})
        if failure == 'http' else response())
    result = native_runtime(url, accounting=recorder).run(
        artifact(), policy=RunPolicy(max_provider_requests=1))
    assert len(requests) == (0 if failure == 'prepare' else 1)
    assert result.exit_code != 0
    assert result.metadata['invocation_budgets_v1']['provider_requests'] == (0 if failure == 'prepare' else 1)
    if failure == 'http':
        assert result.metadata['provider_error_code'] == 'http_error'
    else:
        assert result.metadata['failure_reason'] == 'accounting_failed'


def test_authorization_denial_consumes_tool_attempt(server):
    url, requests, responses = server
    seen = []
    config, tool = setup(url, seen, authorize=lambda args: False)
    responses.extend([completion('', [call(), call()]), completion()])
    result = ProsaicRuntime(config, custom_tools={'lookup_catalog': tool}).run(
        artifact(['lookup_catalog']), policy=RunPolicy(
            allowed_tools=frozenset({'lookup_catalog'}), max_tool_calls=1))
    assert seen == [] and len(requests) == 1
    assert result.metadata['failure_reason'] == 'tool_call_limit'
    assert result.metadata['invocation_budgets_v1']['tool_calls'] == 1


def test_boundary_rechecks_do_not_charge_a_successful_tool_more_than_once(server):
    url, requests, responses = server
    seen = []
    config, tool = setup(url, seen, authorize=lambda args: True)
    responses.extend([completion('', [call()]), completion()])
    result = ProsaicRuntime(config, custom_tools={'lookup_catalog': tool}).run(
        artifact(['lookup_catalog']), policy=RunPolicy(
            allowed_tools=frozenset({'lookup_catalog'}), max_tool_calls=1, max_provider_requests=2))
    assert result.exit_code == 0 and len(seen) == 1 and len(requests) == 2
    assert result.metadata['invocation_budgets_v1'] == {
        'provider_requests': 2, 'tool_calls': 1, 'reported_tokens': 14, 'usage_complete': True}


def test_cancelled_post_tool_boundary_keeps_counts_and_completed_usage(server):
    url, requests, responses = server
    seen = []
    config, tool = setup(url, seen)
    responses.append(completion('', [call()]))
    result = ProsaicRuntime(config, custom_tools={'lookup_catalog': tool}).run(
        artifact(['lookup_catalog']), cancelled=lambda: bool(seen), policy=RunPolicy(
            allowed_tools=frozenset({'lookup_catalog'}), max_tool_calls=1, max_provider_requests=2))
    assert result.exit_code == 130 and len(requests) == 1 and result.token_usage == 7
    assert result.metadata['invocation_budgets_v1']['tool_calls'] == 1


def test_initial_tool_mismatch_does_not_dispatch_or_charge_a_tool(server):
    url, requests, responses = server
    seen = []
    config, tool = setup(url, seen)
    responses.append(completion('', [call(name='write_file')]))
    result = ProsaicRuntime(config, custom_tools={'lookup_catalog': tool}).run(
        artifact(['lookup_catalog']), policy=RunPolicy(
            allowed_tools=frozenset({'lookup_catalog'}), initial_tool='lookup_catalog', max_tool_calls=1))
    assert result.metadata['failure_reason'] == 'tool_choice_not_honored'
    assert seen == [] and len(requests) == 1 and result.token_usage == 7
    assert result.metadata['invocation_budgets_v1']['tool_calls'] == 0


def test_budget_failure_is_a_safe_invocation_observation(server):
    url, _, responses = server
    responses.append(completion())
    records = []
    result = runtime(EndpointConfig(url, 'test', features={'streaming': False})).run(
        artifact(), policy=RunPolicy(max_reported_tokens=6), observer=records.append)
    assert result.exit_code == 1
    assert records[-1]['outcome'] == 'budget_failure' and records[-1]['reason'] == 'token_limit'


@pytest.mark.parametrize('last', [{}, {'prompt_tokens': 2}, {'total_tokens': 3}])
def test_partial_or_conflicting_terminal_stream_snapshot_cannot_restore_allowance(server, last):
    url, requests, responses = server
    chunks = [{'choices': [{'delta': {'content': 'done'}, 'finish_reason': 'stop'}]},
              {'choices': [], 'usage': {'total_tokens': 7}}, {'choices': [], 'usage': last}]
    responses.append(('text/event-stream', (''.join('data: ' + json.dumps(c) + '\n\n'
        for c in chunks) + 'data: [DONE]\n\n').encode()))
    result = runtime(EndpointConfig(url, 'test')).run(artifact(), policy=RunPolicy(max_reported_tokens=7))
    assert len(requests) == 1 and result.exit_code == 1
    assert result.token_usage is None and result.metadata['failure_reason'] == 'usage_unknown'


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_failed_http_usage_stays_unknown_under_a_token_cap(native_server, provider):
    url, requests, responses = native_server
    responses.append((503, 'application/json', b'{"error": {"type": "overloaded_error"}}', {}))
    recorder = MemoryRecorder(provider_id=provider)
    runner = (native_runtime(url, accounting=recorder) if provider == 'anthropic' else
              runtime(EndpointConfig(url, 'test', features={'streaming': False}), accounting=recorder))
    result = runner.run(artifact(), policy=RunPolicy(max_reported_tokens=7))
    assert len(requests) == len(recorder.observations) == 1
    assert result.token_usage is None and result.metadata['failure_reason'] == 'usage_unknown'
    assert result.metadata['provider_error_code'] == 'http_error'
    assert result.metadata['invocation_budgets_v1']['usage_complete'] is False


@pytest.mark.parametrize('accounted', [False, True])
@pytest.mark.parametrize('failure,provider_code,usage_status', [
    ('http', 'http_error', 'unknown'), ('url', 'url_error', 'unknown'),
    ('malformed_sse', 'malformed_sse', 'untrusted'), ('timeout_sse', 'timeout', 'unknown'),
])
def test_failed_followup_retains_prior_sum_without_complete_usage(
        budget_transport, accounted, failure, provider_code, usage_status):
    requests, responses = budget_transport
    responses.append(completion('', [call()]))
    if failure == 'http':
        responses.append(HTTPError('http://fixture.invalid/v1', 503, 'fixture', Message(), io.BytesIO(b'{}')))
    elif failure == 'url':
        responses.append(URLError('fixture transport error'))
    elif failure == 'malformed_sse':
        responses.append(('text/event-stream', b'data: {"choices":\n\n'))
    else:
        responses.append(('text/event-stream', b'', True))
    seen = []
    config, tool = setup('http://fixture.invalid/v1', seen, streaming=True)
    recorder = MemoryRecorder() if accounted else None
    result = ProsaicRuntime(config, custom_tools={'lookup_catalog': tool}, accounting=recorder).run(
        artifact(['lookup_catalog']), env={}, policy=RunPolicy(
            allowed_tools=frozenset({'lookup_catalog'}), max_reported_tokens=20))
    assert len(requests) == 2 and seen == [{'sku': 'SKU-001'}]
    assert result.exit_code == 1 and result.token_usage is None
    assert result.metadata['failure_reason'] == 'usage_unknown'
    assert result.metadata['provider_error_code'] == provider_code
    assert result.timed_out == (failure == 'timeout_sse')
    assert result.metadata['reported_token_usage'] == 7
    assert result.metadata['invocation_budgets_v1'] == {
        'provider_requests': 2, 'tool_calls': 1, 'reported_tokens': 7, 'usage_complete': False}
    if recorder is not None:
        observations = list(recorder.observations.values())
        assert len(observations) == 2
        assert observations[0]['usage']['total_tokens'] == 7
        assert observations[1]['usage']['status'] == usage_status


@pytest.mark.parametrize('accounted', [False, True])
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('with_tools', [False, True])
def test_duplicate_terminal_usage_is_unknown_before_admission(
        budget_transport, accounted, streaming, with_tools):
    requests, responses = budget_transport
    body = b'{"choices":[{"message":{"content":"done"},"finish_reason":"stop"}],"usage":{"total_tokens":100,"total_tokens":7}}'
    responses.append(('text/event-stream', b'data: ' + body + b'\n\ndata: [DONE]\n\n')
        if streaming else ('application/json', body))
    recorder = MemoryRecorder() if accounted else None
    result = runtime(EndpointConfig('http://fixture.invalid/v1', 'test', features={'streaming': streaming}),
        accounting=recorder).run(artifact('read' if with_tools else None), env={},
            policy=RunPolicy(max_reported_tokens=7, read_roots=('.',),
                allowed_tools=frozenset({'read_file'}) if with_tools else frozenset()))
    assert len(requests) == 1 and result.stdout == 'done'
    assert result.exit_code == 1 and result.token_usage is None
    assert result.metadata['failure_reason'] == 'usage_unknown'
    assert result.metadata['invocation_budgets_v1'] == {
        'provider_requests': 1, 'tool_calls': 0, 'reported_tokens': 0, 'usage_complete': False}
    if recorder is not None:
        observation, = recorder.observations.values()
        assert observation['usage']['status'] == 'untrusted'


@pytest.mark.parametrize('accounted', [False, True])
@pytest.mark.parametrize('streaming', [False, True])
def test_duplicate_tool_turn_usage_prevents_handler_and_followup(
        budget_transport, accounted, streaming):
    requests, responses = budget_transport
    kind, body = reply('', [call()], streaming)
    responses.extend([(kind, body.replace(b'"total_tokens": 7', b'"total_tokens": 100, "total_tokens": 7')),
                      reply('done', streaming=streaming)])
    seen = []
    config, tool = setup('http://fixture.invalid/v1', seen, streaming=streaming)
    recorder = MemoryRecorder() if accounted else None
    result = ProsaicRuntime(config, custom_tools={'lookup_catalog': tool}, accounting=recorder).run(
        artifact(['lookup_catalog']), env={}, policy=RunPolicy(
            allowed_tools=frozenset({'lookup_catalog'}), max_reported_tokens=20))
    assert seen == [] and len(requests) == 1
    assert result.exit_code == 1 and result.token_usage is None
    assert result.metadata['failure_reason'] == 'usage_unknown'
    assert result.metadata['invocation_budgets_v1']['tool_calls'] == 0
    if recorder is not None:
        observation, = recorder.observations.values()
        assert observation['usage']['status'] == 'untrusted'


def test_credential_failure_before_followup_attempt_does_not_charge_unknown_usage(budget_transport, tmp_path):
    requests, responses = budget_transport
    responses.append(completion('', [call()]))
    credential = tmp_path / 'fixture-key'
    credential.write_text('fixture-only')
    seen = []
    config, original = setup('http://fixture.invalid/v1', seen)
    config = replace(config, profiles={'local': replace(config.profiles['local'], api_key_file=str(credential))})

    def handler(args):
        seen.append(args)
        credential.unlink()
        return {'found': True}

    tool = CustomTool(original.name, original.description, original.parameters, handler, original.version)
    recorder = MemoryRecorder()
    result = ProsaicRuntime(config, custom_tools={'lookup_catalog': tool}, accounting=recorder).run(
        artifact(['lookup_catalog']), env={}, policy=RunPolicy(
            allowed_tools=frozenset({'lookup_catalog'}), max_reported_tokens=20))
    assert len(requests) == len(recorder.observations) == 1 and seen == [{'sku': 'SKU-001'}]
    assert result.exit_code == 1 and result.metadata['provider_error_code'] == 'api_key_file_error'
    assert result.token_usage == 7 and 'failure_reason' not in result.metadata
    assert result.metadata['invocation_budgets_v1'] == {
        'provider_requests': 1, 'tool_calls': 1, 'reported_tokens': 7, 'usage_complete': True}


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('policy', [RunPolicy(), RunPolicy(max_provider_requests=1)])
def test_uncapped_public_usage_keeps_legacy_duplicate_parsing(budget_transport, streaming, policy):
    requests, responses = budget_transport
    body = b'{"choices":[{"message":{"content":"done"},"finish_reason":"stop"}],"usage":{"total_tokens":100,"total_tokens":7}}'
    responses.append(('text/event-stream', b'data: ' + body + b'\n\ndata: [DONE]\n\n')
        if streaming else ('application/json', body))
    recorder = MemoryRecorder()
    result = runtime(EndpointConfig('http://fixture.invalid/v1', 'test', features={'streaming': streaming}),
        accounting=recorder).run(artifact(), env={}, policy=policy)
    assert len(requests) == 1 and result.exit_code == 0 and result.stdout == 'done'
    assert result.token_usage == 7 and 'failure_reason' not in result.metadata
    observation, = recorder.observations.values()
    assert observation['usage']['status'] == 'untrusted'


@pytest.mark.parametrize('streaming', [False, True])
def test_low_level_default_keeps_legacy_duplicate_parsing(budget_transport, streaming, tmp_path):
    requests, responses = budget_transport
    body = b'{"choices":[{"message":{"content":"done"},"finish_reason":"stop"}],"usage":{"total_tokens":100,"total_tokens":7}}'
    responses.append(('text/event-stream', b'data: ' + body + b'\n\ndata: [DONE]\n\n')
        if streaming else ('application/json', body))
    result = OpenAICompatibleBackend(EndpointConfig('http://fixture.invalid/v1', 'test',
        features={'streaming': streaming})).run_prompt(Invocation(str(tmp_path), 'fixture', {}, 5))
    assert len(requests) == 1 and result.exit_code == 0 and result.stdout == 'done'
    assert result.token_usage == 7 and 'failure_reason' not in result.metadata
    assert 'invocation_budgets_v1' not in result.metadata


def test_uncapped_result_and_descriptor_match_immutable_r1_golden(budget_transport):
    """Literal evidence captured using git archive c7c83a1's source package."""
    requests, responses = budget_transport
    responses.append(completion('done'))
    result = runtime(EndpointConfig('http://fixture.invalid/v1', 'test', features={'streaming': False})).run(
        artifact(), env={}, policy=RunPolicy())
    assert len(requests) == 1
    assert asdict(result) == {'exit_code': 0, 'stdout': 'done', 'stderr': '',
        'token_usage': 7, 'cost_usd': 0.0, 'timed_out': False, 'metadata': {
            'provider': 'openai-compatible', 'request_model': 'test', 'streamed': False,
            'http_status': 200, 'raw_response_headers': {'content-type': 'application/json'},
            'finish_reason': 'stop', 'token_usage_details': {
                'prompt_tokens': 5, 'completion_tokens': 2, 'total_tokens': 7},
            'raw_response_metadata': {}, 'reasoning_content_policy': 'auto',
            'reasoning_content_observed': False, 'token_usage_status': 'reported',
            'artifact_id': 'subagents/test.md',
            'artifact_sha256': '5b828e130ff99d5953b8bda007e903e88907e103677257d848345f2afc2c8e56',
            'profile': 'small', 'cost_status': 'unavailable'}}
    config, tool = setup('http://fixture.invalid/v1', [])
    assert ProsaicRuntime(config, custom_tools={'lookup_catalog': tool}).tool_descriptors['lookup_catalog'] == {
        'name': 'lookup_catalog', 'description': 'Lookup', 'version': 'v1',
        'parameters': {'type': 'object', 'required': ['sku'], 'additionalProperties': False,
            'properties': {'sku': {'type': 'string', 'pattern': '^SKU-[0-9]{3}$'}}},
        'max_argument_bytes': 16384, 'max_result_bytes': 65536, 'authorization_required': False}
