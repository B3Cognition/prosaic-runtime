"""Public turns exercise both real provider transports and the shared loop."""
import json
from dataclasses import asdict, replace

import pytest
from prosaic_runtime import ConversationMessage, InvocationScope, TurnAdmissionError
from support.conversation_server import turn_fixture


def execute(fx, **kwargs):
    rt = fx.runtime
    assert callable(getattr(rt, 'turn', None)), 'public role-aware turn is required'
    return rt.turn(fx.agent, policy=fx.policy, **kwargs)


@pytest.mark.parametrize('streaming', [False, True])
def test_provider_timeout_turn_has_a_persistable_exit_status(turn_fixture, monkeypatch, streaming):
    class Opener:
        def open(self, *args, **kwargs):
            raise TimeoutError('synthetic provider timeout')

    # Replace only network I/O; execute the real provider and turn reducers.
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: Opener())
    with turn_fixture('openai-compatible') as fx:
        fx.streaming = streaming
        result = execute(fx, message='Hello')
        assert result.result.exit_code == 124
        assert result.result.timed_out is True
        assert result.result.stdout == ''
        assert result.completed_turn is None
        assert result.evidence.to_dict()['failureCode'] == 'timeout'
        assert result.evidence.to_dict()['providerAttempts'] == 1


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_failed_corrections_are_unprivileged_in_next_actual_payload(turn_fixture, provider):
    with turn_fixture(provider) as fx:
        fx.queue_text('Prague')
        history = tuple(ConversationMessage.from_dict({'role': 'user',
            'content': [{'type': 'text', 'text': text}], 'turnStatus': 'failed',
            'failureCode': 'providerFailure'}) for text in ('I meant Prague', 'Czechia, please'))
        result = execute(fx, message='Which city?', history=history)
        assert result.result.stdout == 'Prague' and result.result.exit_code == 0
        payload = fx.requests[0]
        system = payload.get('system') or payload['messages'][0]['content']
        assert 'I meant Prague' not in system
        assert 'Which city?' not in system
        messages = json.dumps(payload['messages'])
        assert 'I meant Prague' in messages and 'Czechia, please' in messages and 'failed' in messages
        assert [m.role for m in result.completed_turn.messages] == ['user', 'assistant']


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_prepared_size_matches_actual_wire_and_is_readmitted(turn_fixture, provider):
    with turn_fixture(provider) as fx:
        fx.queue_text('Answer')
        rt = fx.runtime
        assert callable(getattr(rt, 'turn', None)), 'public prepared turn execution is required'
        prepared = rt.prepare_turn(fx.agent, message='ž "\\"', input_data={'choices': ['one', 'two']}, policy=fx.policy)
        result = rt.turn(prepared)
        assert result.result.exit_code == 0
        assert len(fx.raw_requests[0]) == prepared.input_bytes
        with pytest.raises(TurnAdmissionError):
            rt.turn(prepared, message='override')
        rt.config.profiles['local'] = replace(rt.config.profiles['local'], model='changed')
        with pytest.raises(TurnAdmissionError) as exc:
            rt.turn(prepared)
        assert exc.value.code == 'bindingMismatch' and len(fx.requests) == 1


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_tool_history_is_portable_and_never_redispatched(turn_fixture, provider):
    with turn_fixture(provider) as fx:
        effects = []
        fx.add_tool('lookup', lambda args: effects.append(1) or {'city': 'Prague'})
        calls = [{'id': 'call_0', 'name': 'lookup', 'arguments': '{}'}]
        fx.queue_calls(calls)
        fx.queue_text('Prague')
        first = execute(fx, message='Find Prague', operation_context=InvocationScope('first'))
        assert first.result.exit_code == 0 and effects == [1]
        raw = first.completed_turn.to_dict()
        assert 'call_0' not in json.dumps(raw) and 'private-native-id' not in json.dumps(raw)
        fx.queue_calls(calls)
        fx.queue_text('Still Prague')
        second = execute(fx, message='Check once more', history=first.completed_turn.messages,
                         operation_context=InvocationScope('second'))
        assert second.result.stdout == 'Still Prague' and effects == [1, 1]
        first_ids = [b['callId'] for m in raw['messages'] for b in m['content'] if b['type'] == 'toolCall']
        second_ids = [b['callId'] for m in second.completed_turn.to_dict()['messages']
                      for b in m['content'] if b['type'] == 'toolCall']
        assert first_ids and second_ids and set(first_ids).isdisjoint(second_ids)


def test_native_interleaved_text_and_multiple_calls_keep_block_order(turn_fixture):
    with turn_fixture('anthropic') as fx:
        fx.add_tool('lookup', lambda args: {'city': 'Prague'})
        fx.queue_calls([], blocks=[{'type': 'text', 'text': 'First'},
            {'type': 'tool_use', 'id': 'a', 'name': 'lookup', 'input': {}},
            {'type': 'text', 'text': 'Second'},
            {'type': 'tool_use', 'id': 'b', 'name': 'lookup', 'input': {}}])
        fx.queue_text('Done')
        result = execute(fx, message='Find')
        assert result.result.exit_code == 0
        blocks = result.completed_turn.to_dict()['messages'][1]['content']
        assert [b['type'] for b in blocks] == ['text', 'toolCall', 'text', 'toolCall']
        assert [b['text'] for b in blocks if b['type'] == 'text'] == ['First', 'Second']


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_streamed_text_is_provisional_and_result_excludes_provider_secrets(turn_fixture, provider):
    with turn_fixture(provider) as fx:
        events = []
        fx.queue_stream_text('Preview')
        result = execute(fx, message='Hello', on_event=events.append)
        assert result.result.stdout == 'Preview'
        assert any(e.get('event') == 'text_delta' and 'Preview' in e.get('text', '') for e in events)
        assert not any(e.get('event') == 'turnCommitted' for e in events)
        encoded = json.dumps(asdict(result.result))
        assert 'provider-private-secret' not in encoded and 'private-native-id' not in encoded
        assert 'raw_response_headers' not in encoded


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_provider_failure_has_no_completed_assistant_history(turn_fixture, provider):
    with turn_fixture(provider) as fx:
        fx.queue_http_failure(503)
        result = execute(fx, message='Hello')
        assert result.result.exit_code != 0 and result.completed_turn is None
        assert result.result.stderr == 'providerFailure'
        assert 'credentials' not in json.dumps(asdict(result.result))
        assert result.evidence.to_dict()['usageComplete'] is False


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_repeated_native_ids_within_one_turn_are_remapped_per_exchange(turn_fixture, provider):
    with turn_fixture(provider) as fx:
        effects = []
        fx.add_tool('lookup', lambda args: effects.append(1) or {})
        for _ in range(2):
            fx.queue_calls([{'id': 'call_0', 'name': 'lookup', 'arguments': '{}'}])
        fx.queue_text('done')
        result = execute(fx, message='Find')
        assert result.result.exit_code == 0 and effects == [1, 1]
        if provider == 'anthropic':
            ids = [b['id'] for m in fx.requests[-1]['messages'] for b in m['content'] if b['type'] == 'tool_use']
            outcomes = [b['tool_use_id'] for m in fx.requests[-1]['messages'] for b in m['content'] if b['type'] == 'tool_result']
        else:
            ids = [c['id'] for m in fx.requests[-1]['messages'] for c in m.get('tool_calls', [])]
            outcomes = [m['tool_call_id'] for m in fx.requests[-1]['messages'] if m['role'] == 'tool']
        assert len(ids) == len(set(ids)) == 2
        assert outcomes == ids
