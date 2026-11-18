"""Opt-in TOON encoding of tool results in the request sent to the model."""
import json

import pytest
from toon_format import decode

from prosaic_runtime import EndpointConfig, ToolFailure, TurnAdmissionError, TurnLimits
from prosaic_runtime import tool_result_format
from prosaic_runtime.conversation_preparation import native_payload_digest
from support.conversation_server import turn_fixture

ROWS = {'rows': [{'id': 1, 'name': 'alpha'}, {'id': 2, 'name': 'beta'}]}


def tool_contents(request):
    """Tool result texts in a captured request, for either provider's wire shape."""
    found = []
    for message in request['messages']:
        if message.get('role') == 'tool':
            found.append(message['content'])
        elif isinstance(message.get('content'), list):
            found += [block['content'] for block in message['content'] if block.get('type') == 'tool_result']
    return found


def lookup_turn(fx, features, handler=lambda args: ROWS):
    fx.features = features
    fx.add_tool('lookup', handler)
    fx.queue_calls([{'id': 'call_1', 'name': 'lookup', 'arguments': '{}'}])
    fx.queue_text('done')
    return fx.runtime.turn(fx.agent, message='Look it up', policy=fx.policy)


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_toon_profile_sends_tool_results_as_a_table(turn_fixture, provider):
    with turn_fixture(provider) as fx:
        result = lookup_turn(fx, {'tool_result_format': 'toon'})
        assert result.result.exit_code == 0
        [content] = tool_contents(fx.requests[1])
        assert 'rows[2]{id,name}:' in content
        assert decode(content) == {'status': 'ok', 'result': ROWS}


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_default_profile_keeps_json_tool_results(turn_fixture, provider):
    with turn_fixture(provider) as fx:
        lookup_turn(fx, {})
        [content] = tool_contents(fx.requests[1])
        assert json.loads(content) == {'status': 'ok', 'result': ROWS}


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_error_results_stay_json(turn_fixture, provider):
    with turn_fixture(provider) as fx:
        lookup_turn(fx, {'tool_result_format': 'toon'}, handler=lambda args: ToolFailure('not_found'))
        [content] = tool_contents(fx.requests[1])
        assert json.loads(content) == {'status': 'error', 'error': 'not_found'}
        if provider == 'anthropic':
            [block] = [b for m in fx.requests[1]['messages'] if isinstance(m['content'], list)
                       for b in m['content'] if b.get('type') == 'tool_result']
            assert block['is_error'] is True


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_history_results_are_encoded_and_the_first_request_is_still_admitted(turn_fixture, provider):
    with turn_fixture(provider) as fx:
        fx.features = {'tool_result_format': 'toon'}
        fx.add_tool('lookup', lambda args: ROWS)
        history = fx.compaction_history(outcomes=1, older_result_chars=10)
        fx.queue_text('done')
        result = fx.runtime.turn(fx.agent, message='Continue', history=history, policy=fx.policy)
        assert result.result.exit_code == 0
        [content] = tool_contents(fx.requests[0])
        assert decode(content) == {'status': 'ok', 'result': {'content': 'x' * 10}}


def test_encoding_failure_falls_back_to_json(monkeypatch):
    def broken(_value):
        raise ValueError('cannot encode')
    monkeypatch.setattr(tool_result_format, '_encode', broken)
    message = {'role': 'tool', 'tool_call_id': 'c1', 'content': json.dumps({'status': 'ok', 'result': ROWS})}
    [kept] = tool_result_format.encode_tool_results([message], {'tool_result_format': 'toon'})
    assert kept == message


def test_encoding_leaves_the_callers_messages_unchanged():
    message = {'role': 'tool', 'tool_call_id': 'c1', 'content': json.dumps({'status': 'ok', 'result': ROWS})}
    before = dict(message)
    tool_result_format.encode_tool_results([message], {'tool_result_format': 'toon'})
    assert message == before


def test_unknown_format_is_rejected_at_configuration():
    with pytest.raises(ValueError, match='tool_result_format'):
        EndpointConfig('http://localhost/v1', 'm', features={'tool_result_format': 'yaml'})


def test_toon_without_the_library_names_the_extra(monkeypatch):
    monkeypatch.setattr(tool_result_format, '_encode', None)
    with pytest.raises(ValueError, match=r'b3-prosaic-runtime\[toon\]'):
        EndpointConfig('http://localhost/v1', 'm', features={'tool_result_format': 'toon'})


def counting_encoder(monkeypatch):
    calls, real = [], tool_result_format._encode
    def counted(value):
        calls.append(value)
        return real(value)
    monkeypatch.setattr(tool_result_format, '_encode', counted)
    return calls


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_each_result_is_encoded_once_per_run_across_tool_rounds(turn_fixture, provider, monkeypatch):
    calls = counting_encoder(monkeypatch)
    with turn_fixture(provider) as fx:
        fx.features = {'tool_result_format': 'toon'}
        fx.add_tool('lookup', lambda args: ROWS)
        fx.queue_calls([{'id': 'call_1', 'name': 'lookup', 'arguments': '{}'}])
        fx.queue_calls([{'id': 'call_2', 'name': 'lookup', 'arguments': '{}'}])
        fx.queue_text('done')
        result = fx.runtime.turn(fx.agent, message='Look it up twice', policy=fx.policy)
        assert result.result.exit_code == 0
        assert len(tool_contents(fx.requests[2])) == 2
        assert len(calls) == 1  # both rounds returned the same result text; the second request reuses it


def test_cache_is_not_shared_between_calls_without_one(monkeypatch):
    calls = counting_encoder(monkeypatch)
    message = {'role': 'tool', 'tool_call_id': 'c1', 'content': json.dumps({'status': 'ok', 'result': ROWS})}
    tool_result_format.encode_tool_results([message], {'tool_result_format': 'toon'})
    tool_result_format.encode_tool_results([message], {'tool_result_format': 'toon'})
    assert len(calls) == 2


def test_compacted_results_are_encoded_too(turn_fixture, tmp_path):
    with turn_fixture('openai-compatible') as fx:
        fx.features = {'tool_result_format': 'toon', 'compact_after_tool_results': 0,
                       'keep_recent_tool_results': 0, 'compact_tool_result_after_chars': 10}
        fx.add_tool('lookup', lambda args: ROWS)
        fx.queue_calls([{'id': 'call_1', 'name': 'lookup', 'arguments': '{}'}])
        fx.queue_text('done')
        result = fx.runtime.run(fx.agent, cwd=tmp_path, policy=fx.policy)
        assert result.exit_code == 0 and result.metadata['tool_result_compactions'] == 1
        [content] = tool_contents(fx.requests[1])
        compacted = decode(content)
        assert compacted['status'] == 'compacted' and compacted['tool_name'] == 'lookup'


def history_fixture(fx, result_format):
    """A runtime whose profile uses ``result_format``, and a history holding one tool result."""
    fx.features = {'tool_result_format': result_format}
    fx.add_tool('lookup', lambda args: ROWS)
    return fx.runtime, fx.compaction_history(outcomes=1, older_result_chars=10)


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_changing_the_format_after_preparing_is_refused_before_dispatch(turn_fixture, provider):
    with turn_fixture(provider) as fx:
        runtime, history = history_fixture(fx, 'toon')
        prepared = runtime.prepare_turn(fx.agent, message='Continue', history=history, policy=fx.policy)
        fx.features = {'tool_result_format': 'json'}
        with pytest.raises(TurnAdmissionError) as refused:
            fx.runtime.turn(prepared)
        assert refused.value.code == 'bindingMismatch' and fx.requests == []


FREEZABLE = {'input_data': {'selected': 'captured'}, 'input_profile': 'pack-v1'}  # freezing needs an input profile


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_changing_the_format_after_freezing_is_refused_before_dispatch(turn_fixture, provider):
    with turn_fixture(provider) as fx:
        runtime, history = history_fixture(fx, 'toon')
        frozen = runtime.prepare_turn(fx.agent, message='Continue', history=history, policy=fx.policy,
                                      **FREEZABLE).export_frozen_input()
        fx.features = {'tool_result_format': 'json'}
        with pytest.raises(TurnAdmissionError) as refused:
            fx.runtime.restore_turn(fx.agent, history=history, frozen_input=frozen, policy=fx.policy,
                                    limits=TurnLimits())
        assert refused.value.code == 'bindingMismatch' and fx.requests == []


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_frozen_toon_history_restores_and_sends_the_admitted_request(turn_fixture, provider):
    with turn_fixture(provider) as fx:
        runtime, history = history_fixture(fx, 'toon')
        prepared = runtime.prepare_turn(fx.agent, message='Continue', history=history, policy=fx.policy, **FREEZABLE)
        restored = fx.runtime.restore_turn(fx.agent, history=history, frozen_input=prepared.export_frozen_input(),
                                           policy=fx.policy, limits=TurnLimits())
        assert restored.context_identity == prepared.context_identity
        fx.queue_text('done')
        assert fx.runtime.turn(restored).result.exit_code == 0
        assert len(fx.raw_requests[0]) == prepared.input_bytes
        assert native_payload_digest(fx.requests[0]) == json.loads(restored._encoded)['nativePayloadDigest']
        [content] = tool_contents(fx.requests[0])
        assert decode(content) == {'status': 'ok', 'result': {'content': 'x' * 10}}
