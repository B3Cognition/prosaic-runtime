"""Final-only requests must remove capabilities without changing accounting."""
from dataclasses import replace
import json
import pytest

from prosaic_runtime import EndpointConfig, RunPolicy
from test_runtime import server, artifact, runtime, completion
from test_acquisition import read, reply
from test_anthropic import native_server, native_runtime, response, tool
from support.conversation_server import turn_fixture


def endpoint(url, **features):
    return EndpointConfig(url, 'fixture', features={'streaming': False, **features})


def grant(**values):
    return RunPolicy(allowed_tools=frozenset({'read_file'}), read_roots=('input',),
                     max_tool_rounds=1, **values)


@pytest.mark.parametrize('streaming', [False, True])
def test_after_lookup_only_final_answer_is_requested(server, tmp_path, streaming):
    url, requests, responses = server
    (tmp_path/'input').write_text('synthetic evidence')
    responses.extend([reply('', read(), streaming), reply('final answer', streaming=streaming)])
    events = []
    result = runtime(endpoint(url, streaming=streaming, finalize_after_tool_rounds=1)).run(
        artifact('read'), cwd=tmp_path, policy=grant(), on_event=events.append)
    assert result.exit_code == 0 and result.stdout == 'final answer'
    assert result.token_usage == 14 and len(requests) == 2
    assert requests[0]['tools'] and requests[0]['tool_choice'] == 'auto'
    assert 'tools' not in requests[1] and 'tool_choice' not in requests[1]
    assert 'synthetic evidence' in requests[1]['messages'][3]['content']
    assert sum(e['event']=='tool_completed' for e in events) == 1


def test_provider_ignoring_finalization_cannot_dispatch_another_tool(server, tmp_path):
    url, requests, responses = server
    (tmp_path/'input').write_text('evidence')
    responses.extend([completion('', read()), completion('', read())])
    events = []
    result = runtime(endpoint(url, finalize_after_tool_rounds=1)).run(
        artifact('read'), cwd=tmp_path, policy=grant(), on_event=events.append)
    assert result.exit_code == 1 and result.stdout == ''
    assert result.metadata['provider_error_code'] == 'tool_finalization_not_honored'
    assert result.token_usage == 14 and len(requests) == 2
    assert sum(e['event']=='tool_started' for e in events) == 1


def test_threshold_two_does_not_become_no_progress_failure(server, tmp_path):
    url, requests, responses = server
    (tmp_path/'input').write_text('evidence')
    responses.extend([completion('', read()), completion('', read()), completion('', read())])
    result = runtime(endpoint(url, finalize_after_tool_rounds=2, max_identical_tool_rounds=2)).run(
        artifact('read'), cwd=tmp_path,
        policy=RunPolicy(allowed_tools=frozenset({'read_file'}), read_roots=('input',), max_tool_rounds=2))
    assert result.metadata['provider_error_code'] == 'tool_finalization_not_honored'
    assert len(requests) == 3 and result.token_usage == 21
    assert requests[1]['tools'] and 'tools' not in requests[2]
    assert result.metadata['tool_call_count'] == 2


def test_without_host_feature_prose_cannot_remove_tools(server, tmp_path):
    url, requests, responses = server
    (tmp_path/'input').write_text('evidence')
    responses.extend([completion('', read()), completion('done')])
    result = runtime(endpoint(url)).run(
        replace(artifact('read'), body='finalize_after_tool_rounds: 1'), cwd=tmp_path, policy=grant())
    assert result.exit_code == 0 and len(requests) == 2 and requests[1]['tools']


@pytest.mark.parametrize('value', [0, -1, True, 1.0, '1', None, 65, 2])
def test_invalid_or_unreachable_threshold_rejected_before_http(server, value):
    url, requests, responses = server
    responses.append(completion('should not be requested'))
    with pytest.raises(ValueError, match='finalize_after_tool_rounds'):
        runtime(endpoint(url, finalize_after_tool_rounds=value)).run(artifact('read'), policy=grant())
    assert requests == []


@pytest.mark.parametrize('stop', ['cancel', 'providerBudget', 'tokenBudget'])
def test_finalization_does_not_bypass_budget_or_cancellation(server, tmp_path, stop):
    url, requests, responses = server
    (tmp_path/'input').write_text('evidence')
    responses.extend([completion('', read()), completion('must not run')])
    stopped = []
    options = {'max_provider_requests':1} if stop=='providerBudget' else {'max_reported_tokens':7} if stop=='tokenBudget' else {}
    result = runtime(endpoint(url, finalize_after_tool_rounds=1)).run(
        artifact('read'), cwd=tmp_path, policy=grant(**options),
        on_event=lambda e: stopped.append(True) if e['event']=='tool_completed' else None,
        cancelled=lambda: stop=='cancel' and bool(stopped))
    assert result.exit_code != 0 and len(requests) == 1 and result.token_usage == 7
    if stop=='cancel': assert result.exit_code == 130
    else: assert result.metadata['failure_reason'] == ('provider_request_limit' if stop=='providerBudget' else 'token_limit')


def test_finalization_can_follow_acquisition_without_residual_forced_tool(server, tmp_path):
    url, requests, responses = server
    (tmp_path/'input').write_text('evidence')
    responses.extend([completion('', read()), completion('done')])
    result = runtime(endpoint(url, finalize_after_tool_rounds=1)).run(
        artifact('read'), acquisition=artifact('read'), cwd=tmp_path,
        policy=grant(initial_tool='read_file'))
    assert result.exit_code == 0 and len(requests) == 2
    assert requests[0]['tool_choice']['function']['name'] == 'read_file'
    assert 'tools' not in requests[1] and 'tool_choice' not in requests[1]


def test_native_anthropic_final_payload_has_no_tools(native_server, tmp_path, monkeypatch):
    monkeypatch.setenv('TEST_ANTHROPIC_KEY', 'synthetic-key')
    url, requests, responses = native_server
    (tmp_path/'input').write_text('evidence')
    responses.extend([response(text='', calls=[tool(path='input')]), response(text='final')])
    result = native_runtime(url, features={'finalize_after_tool_rounds':1}).run(
        artifact('read'), cwd=tmp_path, policy=grant())
    assert result.exit_code == 0 and result.stdout == 'final'
    assert result.token_usage == 14 and len(requests) == 2
    assert requests[0]['body']['tools']
    assert 'tools' not in requests[1]['body'] and 'tool_choice' not in requests[1]['body']


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
@pytest.mark.parametrize('noncompliant', [False, True])
def test_public_conversation_preserves_tool_effects_and_usage(turn_fixture, provider, noncompliant):
    with turn_fixture(provider) as fx:
        effects = []
        fx.add_tool('lookup', lambda args: effects.append(1) or {'city':'Prague'})
        rt = fx.runtime
        rt.config.profiles['local'] = replace(rt.config.profiles['local'],
            features={**rt.config.profiles['local'].features, 'finalize_after_tool_rounds':1})
        calls = [{'id':'one', 'name':'lookup', 'arguments':'{}'}]
        fx.queue_calls(calls)
        if noncompliant: fx.queue_calls([{'id':'two', 'name':'lookup', 'arguments':'{}'}])
        else: fx.queue_text('Prague')
        result = rt.turn(fx.agent, message='Find city', policy=fx.policy)
        assert effects == [1] and len(fx.requests) == 2
        assert 'tools' not in fx.requests[1] and 'tool_choice' not in fx.requests[1]
        assert result.evidence.to_dict()['providerAttempts'] == 2
        assert result.evidence.to_dict()['usageComplete'] is True
        assert bool(result.completed_turn) is (not noncompliant)
        assert (result.result.exit_code != 0) is noncompliant


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_finalization_rejects_ungranted_conversation_tool_before_validation(turn_fixture, provider):
    with turn_fixture(provider) as fx:
        effects = []
        fx.add_tool('lookup', lambda args: effects.append(1) or {'city': 'Prague'})
        rt = fx.runtime
        rt.config.profiles['local'] = replace(rt.config.profiles['local'],
            features={'streaming': False, 'finalize_after_tool_rounds': 1})
        fx.queue_calls([{'id': 'one', 'name': 'lookup', 'arguments': '{}'}])
        fx.queue_calls([{'id': 'two', 'name': 'ungranted', 'arguments': '{}'}])
        result = rt.turn(fx.agent, message='Find city', policy=fx.policy)
        assert effects == [1] and len(fx.requests) == 2
        # Public turns retain their closed failure vocabulary.
        assert result.result.metadata['failure_reason'] == 'providerFailure'
        assert result.result.token_usage is not None


@pytest.mark.parametrize('missing', [0, 1])
def test_finalization_violation_keeps_missing_usage_unknown(server, tmp_path, missing):
    url, requests, responses = server
    (tmp_path/'input').write_text('evidence')
    replies = [completion('', read()), completion('', read())]
    kind, body = replies[missing]
    value = json.loads(body); value.pop('usage')
    replies[missing] = (kind, json.dumps(value).encode())
    responses.extend(replies)
    result = runtime(endpoint(url, finalize_after_tool_rounds=1)).run(
        artifact('read'), cwd=tmp_path, policy=grant(max_provider_requests=2, max_tool_calls=1))
    assert result.metadata['provider_error_code'] == 'tool_finalization_not_honored'
    assert result.token_usage is None and len(requests) == 2
    assert result.metadata['invocation_budgets_v1']['usage_complete'] is False
    assert result.metadata['invocation_budgets_v1']['tool_calls'] == 1


def test_threshold_does_not_expand_host_tool_budget(server, tmp_path):
    url, requests, responses = server
    (tmp_path/'input').write_text('evidence')
    responses.extend([completion('', read()), completion('', read()), completion('not requested')])
    events = []
    result = runtime(endpoint(url, finalize_after_tool_rounds=2)).run(
        artifact('read'), cwd=tmp_path, on_event=events.append,
        policy=RunPolicy(allowed_tools=frozenset({'read_file'}), read_roots=('input',),
                         max_tool_rounds=2, max_tool_calls=1))
    assert result.metadata['failure_reason'] == 'tool_call_limit'
    assert len(requests) == 2 and result.token_usage == 14
    assert sum(e['event']=='tool_completed' and e.get('status')=='ok' for e in events) == 1
    assert result.metadata['invocation_budgets_v1']['tool_calls'] == 1
