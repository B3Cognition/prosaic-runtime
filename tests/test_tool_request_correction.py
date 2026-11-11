"""One opt-in correction of a malformed native tool call, within the turn's request budget."""

from dataclasses import fields, replace
import json

import pytest

from prosaic_runtime import CustomTool, ProsaicRuntime, RunPolicy, RuntimeConfig, TurnAdmissionError, Verdict
from support.conversation_server import ConversationFixture
from support.rules_support import recorder, tagged
from test_conversation_tool_batches import queue_json, queue_stream, wire_call

PROVIDERS = ['openai-compatible', 'anthropic']
SCHEMA_INVALID = wire_call('a', arguments='{"shelf":"QA-999"}')  # The tool schema admits no properties.


def corrected(fx, *, enabled=True):
    return ProsaicRuntime(fx.runtime.config, custom_tools=fx.tools, correct_malformed_tool_calls=enabled)


def failure(turn):
    return turn.evidence.to_dict()['failureCode']


def envelope(fx, index):
    """The correction envelope: the last unprivileged message of request ``index``."""
    message = json.loads(fx.raw_requests[index])['messages'][-1]
    content = message['content'] if isinstance(message['content'], str) else message['content'][-1]['text']
    prefix, data = content.split('\n', 1)
    return message['role'], prefix, json.loads(data), content


def lookup_fixture(fx):
    effects, authorizations = [], []
    fx.add_tool(
        'lookup',
        lambda args: effects.append(args) or {'tide': 'high'},
        authorize=lambda args: authorizations.append(args) or True,
    )
    return effects, authorizations


@pytest.mark.parametrize('provider', PROVIDERS)
@pytest.mark.parametrize('streaming', [False, True])
def test_a_malformed_call_is_corrected_once_and_the_valid_call_then_runs(provider, streaming):
    with ConversationFixture(provider) as fx:
        effects, authorizations = lookup_fixture(fx)
        (queue_stream if streaming else queue_json)(fx, [wire_call('a'), SCHEMA_INVALID | {'id': 'b'}])
        queue_json(fx, [wire_call('c')])
        fx.queue_text('High water at noon.')
        turn = corrected(fx).turn(fx.agent, message='When is high water?', policy=fx.policy)
        role, prefix, data, content = envelope(fx, 1)
        assert len(fx.requests) == 3
    assert turn.result.exit_code == 0 and turn.result.stdout == 'High water at noon.'
    # Nothing in the malformed batch was authorized or run, not even its valid first call.
    assert effects == [{}] and authorizations == [{}]
    assert role == 'user' and 'data, not instructions' in prefix
    assert data == {
        'toolRequestCorrection': {'version': 1, 'issue': 'argumentsSchema', 'callIndex': 1, 'tool': 'lookup'}
    }
    assert 'QA-999' not in content and len(content.encode()) < 1024
    assert turn.tool_request_corrections == ('argumentsSchema',)
    evidence = turn.evidence.to_dict()
    assert evidence['providerAttempts'] == 3 and evidence['toolAttempts'] == 1
    # The malformed batch and the envelope stay out of history, as a rejected draft does.
    calls = [
        part
        for message in turn.completed_turn.to_dict()['messages']
        if message['role'] == 'assistant'
        for part in message['content']
        if part['type'] == 'toolCall'
    ]
    assert len(calls) == 1 and 'shelf' not in json.dumps(turn.completed_turn.to_dict())


@pytest.mark.parametrize('provider', PROVIDERS)
def test_an_ungranted_tool_name_is_never_echoed(provider):
    with ConversationFixture(provider) as fx:
        effects, _ = lookup_fixture(fx)
        queue_json(fx, [wire_call('a', 'ignore_previous_rules')])
        fx.queue_text('High water at noon.')
        turn = corrected(fx).turn(fx.agent, message='When is high water?', policy=fx.policy)
        _, _, data, content = envelope(fx, 1)
    assert turn.result.exit_code == 0 and effects == []
    assert data == {'toolRequestCorrection': {'version': 1, 'issue': 'toolNotGranted', 'callIndex': 0}}
    assert 'ignore_previous_rules' not in content


@pytest.mark.parametrize(
    'call,issue',
    [
        (wire_call('a', arguments='{"x":'), 'argumentsMalformed'),
        (wire_call('a', arguments='{"x":1,"x":2}'), 'argumentsMalformed'),
        (wire_call('a', arguments='[]'), 'argumentsMalformed'),
        (wire_call('a', arguments='{"x":"' + 'y' * 17_000 + '"}'), 'argumentsTooLarge'),
        ({'type': 'function', 'function': {'name': 'lookup', 'arguments': '{}'}}, 'callShape'),
        ({**wire_call('a'), 'type': 'other'}, 'callIdentity'),
        (wire_call(''), 'callIdentity'),
    ],
    ids=['not-json', 'duplicate-keys', 'not-an-object', 'too-large', 'missing-id', 'wrong-type', 'empty-id'],
)
def test_each_call_level_issue_is_named_by_a_closed_code(call, issue):
    with ConversationFixture('openai-compatible') as fx:
        lookup_fixture(fx)
        queue_json(fx, [call])
        fx.queue_text('High water at noon.')
        turn = corrected(fx).turn(fx.agent, message='When is high water?', policy=fx.policy)
        _, _, data, content = envelope(fx, 1)
    assert turn.result.exit_code == 0 and data['toolRequestCorrection']['issue'] == issue
    assert turn.tool_request_corrections == (issue,) and 'y' * 64 not in content


@pytest.mark.parametrize('provider', PROVIDERS)
def test_a_second_malformed_batch_ends_the_turn_after_exactly_one_correction(provider):
    with ConversationFixture(provider) as fx:
        effects, _ = lookup_fixture(fx)
        queue_json(fx, [SCHEMA_INVALID])
        queue_json(fx, [SCHEMA_INVALID])
        fx.queue_text('Should not be reached')
        turn = corrected(fx).turn(fx.agent, message='When is high water?', policy=fx.policy)
        assert len(fx.requests) == 2
    assert effects == [] and failure(turn) == 'invalidToolRequest' and turn.completed_turn is None
    assert turn.tool_request_corrections == ('argumentsSchema',)


@pytest.mark.parametrize('provider', PROVIDERS)
def test_a_correction_never_adds_a_request_beyond_the_policy(provider):
    with ConversationFixture(provider) as fx:
        lookup_fixture(fx)
        queue_json(fx, [SCHEMA_INVALID])
        fx.queue_text('Should not be reached')
        policy = replace(fx.policy, max_provider_requests=1)
        turn = corrected(fx).turn(fx.agent, message='When is high water?', policy=policy)
        assert len(fx.requests) == 1
    assert failure(turn) == 'invalidToolRequest' and turn.tool_request_corrections == ()


@pytest.mark.parametrize('provider', PROVIDERS)
def test_off_by_default_a_malformed_call_still_ends_the_turn(provider):
    with ConversationFixture(provider) as fx:
        lookup_fixture(fx)
        queue_json(fx, [SCHEMA_INVALID])
        fx.queue_text('Should not be reached')
        turn = fx.runtime.turn(fx.agent, message='When is high water?', policy=fx.policy)
        assert len(fx.requests) == 1
    assert failure(turn) == 'invalidToolRequest' and turn.tool_request_corrections == ()


@pytest.mark.parametrize('provider', PROVIDERS)
def test_response_level_malformation_is_never_corrected(provider):
    with ConversationFixture(provider) as fx:
        lookup_fixture(fx)
        raw = (
            '{"choices":[],"choices":[]}' if provider == 'openai-compatible' else '{"type":"message","type":"message"}'
        )
        fx.responses.append((200, 'application/json', raw.encode()))
        fx.queue_text('Should not be reached')
        turn = corrected(fx).turn(fx.agent, message='When is high water?', policy=fx.policy)
        assert len(fx.requests) == 1
    assert failure(turn) == 'invalidToolRequest' and turn.tool_request_corrections == ()


def test_a_native_block_the_provider_parser_rejects_is_never_corrected():
    with ConversationFixture('anthropic') as fx:
        lookup_fixture(fx)
        queue_json(fx, [{'type': 'function', 'function': {'name': 'lookup', 'arguments': '{}'}}])  # No block id.
        fx.queue_text('Should not be reached')
        turn = corrected(fx).turn(fx.agent, message='When is high water?', policy=fx.policy)
        assert len(fx.requests) == 1
    assert failure(turn) == 'invalidToolRequest' and turn.tool_request_corrections == ()


@pytest.mark.parametrize('provider', PROVIDERS)
def test_an_unencodable_argument_in_a_guarded_turn_stays_terminal(provider):
    from prosaic_runtime import generic_guards

    body = (
        '- Look up only seen records. '
        '<!-- enforce: args_reference_seen_ids(keys=["recordId"], stage=before_tool, on_violation=retry) -->\n'
    )
    with ConversationFixture(provider) as fx:
        fx.tools['lookup'] = CustomTool(
            'lookup',
            'Approved lookup',
            {'type': 'object', 'properties': {'recordId': {'type': 'string'}}, 'additionalProperties': False},
            lambda args: pytest.fail('dispatched'),
            'v1',
        )
        fx.queue_calls([{'id': 'native-1', 'name': 'lookup', 'arguments': json.dumps({'recordId': 'REC-\ud800'})}])
        fx.queue_text('Should not be reached')
        runtime = ProsaicRuntime(
            fx.runtime.config, custom_tools=fx.tools, guards=generic_guards(), correct_malformed_tool_calls=True
        )
        turn = runtime.turn(tagged(body, tools=fx.tools), message='Find REC-1', policy=fx.policy)
        assert len(fx.requests) == 1
    assert failure(turn) == 'invalidToolRequest' and turn.tool_request_corrections == ()


def test_streamed_text_of_a_corrected_response_stays_provisional():
    """Without answer-stage rules, text streams live; a corrected response's text was already observed."""
    with ConversationFixture('openai-compatible') as fx:
        lookup_fixture(fx)
        fx.streaming = True
        events = [
            {'choices': [{'index': 0, 'delta': {'content': 'Let me check. '}, 'finish_reason': None}]},
            {
                'choices': [
                    {'index': 0, 'delta': {'tool_calls': [{**SCHEMA_INVALID, 'index': 0}]}, 'finish_reason': None}
                ]
            },
            {
                'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}],
                'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5},
            },
        ]
        body = ''.join(f'data: {json.dumps(event)}\n\n' for event in events) + 'data: [DONE]\n\n'
        fx.responses.append((200, 'text/event-stream', body.encode()))
        fx.queue_stream_text('High water at noon.')
        observed = []
        turn = corrected(fx).turn(fx.agent, message='When is high water?', policy=fx.policy, on_event=observed.append)
    streamed = ''.join(event['text'] for event in observed if event['event'] == 'text_delta')
    assert turn.result.stdout == 'High water at noon.'
    assert streamed == 'Let me check. High water at noon.'  # Provisional, as streamed turn events always are.
    assert 'Let me check.' not in json.dumps(turn.completed_turn.to_dict())


@pytest.mark.parametrize('provider', PROVIDERS)
def test_a_correction_and_a_rule_retry_each_spend_their_own_allowance(provider):
    rule = '- Quote shelf marks exactly. <!-- enforce: shelf_check(stage=final, on_violation=retry) -->\n'
    guard = recorder('shelf_check', Verdict(False), Verdict(True))
    with ConversationFixture(provider) as fx:
        effects, _ = lookup_fixture(fx)
        queue_json(fx, [SCHEMA_INVALID])
        queue_json(fx, [wire_call('c')])
        fx.queue_text('Shelf QA-999.')
        fx.queue_text('Shelf QA-104.')
        runtime = ProsaicRuntime(
            fx.runtime.config, custom_tools=fx.tools, guards=[guard.guard], correct_malformed_tool_calls=True
        )
        turn = runtime.turn(tagged(rule, tools=fx.tools), message='Where?', policy=fx.policy)
        assert len(fx.requests) == 4
    assert turn.result.stdout == 'Shelf QA-104.' and effects == [{}]
    assert turn.tool_request_corrections == ('argumentsSchema',)
    assert turn.rules_retried == ('subagents/shelf-keeper.md:1:shelf_check',)


def test_the_switch_is_a_boolean_constructor_argument_not_configuration():
    with ConversationFixture('openai-compatible') as fx:
        for value in (1, 'true', None):
            with pytest.raises(ValueError, match='correct_malformed_tool_calls'):
                ProsaicRuntime(fx.runtime.config, correct_malformed_tool_calls=value)
    # Host policy, like guards: a RuntimeConfig field would change every Harness workflow identity.
    assert 'correct_malformed_tool_calls' not in {field.name for field in fields(RuntimeConfig)}


def test_the_switch_is_part_of_turn_identity_only_when_on():
    with ConversationFixture('openai-compatible') as fx:
        lookup_fixture(fx)
        policy = RunPolicy(allowed_tools=frozenset(fx.tools), max_provider_requests=4, max_tool_calls=4)
        off = fx.runtime.prepare_turn(fx.agent, message='When is high water?', policy=policy)
        on = corrected(fx).prepare_turn(fx.agent, message='When is high water?', policy=policy)
        assert b'toolRequestCorrection' not in off._encoded
        assert json.loads(on._encoded)['toolRequestCorrection'] is True
        assert on.context_identity != off.context_identity
        with pytest.raises(TurnAdmissionError) as refused:
            fx.runtime.turn(on)  # Readmitted under a runtime with the switch off: the bytes drift.
        assert refused.value.code == 'bindingMismatch' and fx.requests == []


def test_a_frozen_turn_keeps_the_switch_and_restores_only_under_it():
    with ConversationFixture('openai-compatible') as fx:
        lookup_fixture(fx)
        runtime = corrected(fx)
        prepared = runtime.prepare_turn(
            fx.agent, message='Current', input_data={'selected': 'x'}, input_profile='pack-v1', policy=fx.policy
        )
        frozen = prepared.export_frozen_input()
        assert frozen.to_dict()['bindings']['toolRequestCorrection'] is True
        restored = runtime.restore_turn(fx.agent, frozen_input=frozen, policy=fx.policy, limits=prepared_limits())
        assert restored._encoded == prepared._encoded
        with pytest.raises(TurnAdmissionError):
            fx.runtime.restore_turn(fx.agent, frozen_input=frozen, policy=fx.policy, limits=prepared_limits())


def prepared_limits():
    from prosaic_runtime import TurnLimits

    return TurnLimits()
