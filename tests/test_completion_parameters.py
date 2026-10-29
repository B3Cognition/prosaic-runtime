"""Operator-selected wire dialect keeps the same bounded generation contract."""
import json
from dataclasses import replace
import pytest
from prosaic_runtime import EndpointConfig, RuntimeConfig, ProsaicRuntime, RunPolicy
from prosaic_runtime.openai_compatible import OpenAICompatibleBackend
from prosaic_runtime.types import Invocation
from test_runtime import server, runtime, artifact, completion
from test_conformance import openai_stream
from test_token_admission import budget
from support.context_counter import fixture_context


@pytest.mark.parametrize('parameter', ['max_tokens', 'max_completion_tokens'])
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('tools', [False, True])
def test_selected_output_limit_reaches_real_transport_without_legacy_alias(server, parameter, streaming, tools):
    url, requests, responses = server
    responses.append(openai_stream('done') if streaming else completion())
    endpoint = EndpointConfig(url, 'test', temperature=None, max_tokens=37,
        features={'streaming': streaming, 'completion_token_parameter': parameter, 'reasoning_effort': 'none'})
    result = runtime(endpoint).run(artifact(['read_file'] if tools else []))
    assert result.exit_code == 0 and result.stdout == 'done'
    assert result.token_usage == 7
    assert requests[0][parameter] == 37
    assert set(requests[0]) & {'max_tokens', 'max_completion_tokens'} == {parameter}
    assert 'temperature' not in requests[0]
    assert requests[0]['reasoning_effort'] == 'none'


@pytest.mark.parametrize('parameter', [None, True, [], 'max_output_tokens', 'other'])
def test_invalid_completion_dialect_is_rejected_before_network(parameter):
    with pytest.raises(ValueError, match='completion_token_parameter'):
        EndpointConfig('http://localhost/v1', 'test', features={'completion_token_parameter': parameter})


def test_anthropic_cannot_select_openai_completion_dialect():
    with pytest.raises(ValueError, match='completion_token_parameter'):
        EndpointConfig('http://localhost/v1', 'test', provider='anthropic',
            features={'completion_token_parameter': 'max_completion_tokens'})


def test_constrained_request_uses_operator_dialect_and_reasoning_setting(server, tmp_path):
    url, requests, responses = server
    responses.append(completion())
    backend = OpenAICompatibleBackend(EndpointConfig(url, 'test', temperature=None, max_tokens=37,
        features={'completion_token_parameter': 'max_completion_tokens', 'reasoning_effort': 'none'}))
    result = backend.run_constrained_prompt(Invocation(str(tmp_path), 'Return done.', {}, 10),
        model='test', screen_output=lambda b: b, max_input_bytes=1024, max_capture_bytes=1024)
    assert result.exit_code == 0
    assert requests[0]['max_completion_tokens'] == 37
    assert 'max_tokens' not in requests[0] and 'temperature' not in requests[0]
    assert requests[0]['reasoning_effort'] == 'none'


def test_modern_dialect_reserves_and_settles_strict_token_budget(server):
    url, requests, responses = server
    responses.append(completion('yes'))
    b = budget(8)
    endpoint = EndpointConfig(url, 'test', max_tokens=5, features={
        'streaming': False, 'completion_token_parameter': 'max_completion_tokens'})
    with b.activate():
        result = runtime(endpoint).run(artifact())
    assert result.exit_code == 0
    assert b.reported_tokens == 7 and b.reserved_tokens == 0 and b.usage_complete
    assert requests[0]['max_completion_tokens'] == 5


def test_modern_dialect_does_not_bypass_insufficient_budget(server):
    url, requests, responses = server
    b = budget(7)
    with b.activate():
        result = runtime(EndpointConfig(url, 'test', max_tokens=5, features={
            'streaming': False, 'completion_token_parameter': 'max_completion_tokens'})).run(artifact())
    assert result.metadata['failure_reason'] == 'token_limit'
    assert not requests and b.provider_requests == 0


def test_context_counter_sees_modern_limit_clamped_to_output_reservation(server):
    url, requests, responses = server
    seen = []
    policy, counters = fixture_context(model='test', reserve=37, count=lambda request: seen.append(request) or 1)
    endpoint = EndpointConfig(url, 'test', max_tokens=2048, temperature=None,
        features={'streaming': False, 'completion_token_parameter': 'max_completion_tokens'})
    runner = runtime(endpoint)
    agent = replace(artifact(), body='Return grounded output.')
    prepared = runner.prepare_turn(agent, message='Now', context_policy=policy, token_counters=counters)
    assert seen[-1]['max_completion_tokens'] == 37 and 'max_tokens' not in seen[-1]
    assert prepared.context_report.input_tokens == 1 and not requests


def test_prompt_metadata_cannot_switch_operator_token_dialect(server, tmp_path):
    url, requests, responses = server
    responses.append(completion())
    endpoint = EndpointConfig(url, 'test', max_tokens=37, features={
        'streaming': False, 'completion_token_parameter': 'max_completion_tokens'})
    result = OpenAICompatibleBackend(endpoint).run_prompt(Invocation(str(tmp_path), 'Return done.', {}, 10,
        {'completion_token_parameter': 'max_tokens', 'max_completion_tokens': 99999}))
    assert result.exit_code == 0
    assert requests[0]['max_completion_tokens'] == 37 and 'max_tokens' not in requests[0]


def test_yaml_dialect_is_owned_and_changes_profile_qualification_identity(tmp_path):
    from prosaic_runtime.conformance import _profile_fingerprint
    p = tmp_path/'runtime.yml'
    p.write_text('default_profile: sol\nprofiles:\n  sol:\n    base_url: https://api.openai.com/v1\n    model: gpt-6-sol\n    features:\n      completion_token_parameter: max_completion_tokens\n')
    endpoint = RuntimeConfig.load(p).profiles['sol']
    assert _profile_fingerprint(endpoint) != _profile_fingerprint(replace(endpoint, features={}))
