"""Wire and admission contract for the opt-in Nemotron lookup correction."""
from dataclasses import replace

import pytest

from prosaic_runtime import CustomTool, ProsaicRuntime, TurnAdmissionError
from support.conversation_server import ConversationFixture
from test_conversation_tool_batches import queue_json, wire_call

MODEL = 'NVIDIA-Nemotron-3.5-Lightning-30B-A3B-MLX-4bit'
FEATURE = 'omlx_nemotron_tool_correction'
CHOICE = {'choice': ['<tool_call>\n<function=lookup>\n</function>\n</tool_call>']}
INVALID = wire_call('bad', arguments='{"extra":true}')


def runtime(fx, *, enabled=True, correction=True, **features):
    config = fx.runtime.config
    endpoint = config.profiles['local']
    profile = replace(endpoint, model=MODEL, features={**endpoint.features, FEATURE: enabled, **features})
    return ProsaicRuntime(replace(config, profiles={'local': profile}), custom_tools=fx.tools,
                          correct_malformed_tool_calls=correction)


@pytest.mark.parametrize('initial_tool', [None, 'lookup'])
def test_grammar_only_on_correction_and_native_lookup_is_authorized(initial_tool):
    with ConversationFixture('openai-compatible') as fx:
        authorized, reads = [], []
        fx.add_tool('lookup', lambda args: reads.append(args) or {'value': 7},
                    authorize=lambda args: authorized.append(args) or True)
        queue_json(fx, [INVALID])
        queue_json(fx, [wire_call('valid')])
        fx.queue_text('{"value":7}')
        turn = runtime(fx, json_mode=True).turn(fx.agent, message='Read the record',
            policy=replace(fx.policy, initial_tool=initial_tool, max_provider_requests=3, max_tool_calls=1))
    first, correction, continuation = fx.requests
    assert correction['structured_outputs'] == CHOICE
    assert 'structured_outputs' not in first and 'structured_outputs' not in continuation
    assert 'response_format' in first and 'response_format' in continuation and 'response_format' not in correction
    assert correction['tool_choice'] == first['tool_choice']
    assert turn.result.exit_code == 0 and turn.result.stdout == '{"value":7}'
    assert authorized == reads == [{}]
    assert turn.tool_request_corrections == ('argumentsSchema',)
    assert turn.evidence.to_dict()['providerAttempts'] == 3


@pytest.mark.parametrize('response', ['text', 'invalid', 'denied', 'http_error'])
def test_constraint_never_executes_an_invalid_or_unauthorized_read(response):
    with ConversationFixture('openai-compatible') as fx:
        authorized, reads = [], []
        fx.add_tool('lookup', lambda args: reads.append(args),
                    authorize=lambda args: authorized.append(args) or False)
        queue_json(fx, [INVALID])
        if response == 'text':
            fx.queue_text('{"value":7}')
        elif response == 'http_error':
            fx.queue_http_failure(400)
        else:
            queue_json(fx, [INVALID if response == 'invalid' else wire_call('valid')])
            if response == 'denied':
                fx.queue_text('Lookup denied.')
        turn = runtime(fx).turn(fx.agent, message='Read the record',
                                policy=replace(fx.policy, max_provider_requests=3))
    assert fx.requests[1]['structured_outputs'] == CHOICE
    assert len(fx.requests) == (3 if response == 'denied' else 2)
    if response != 'denied':
        assert turn.result.exit_code == (400 if response == 'http_error' else 1)
        assert turn.completed_turn is None
    assert not reads and authorized == ([{}] if response == 'denied' else [])
    assert turn.tool_request_corrections == ('argumentsSchema',)


@pytest.mark.parametrize('shape', ['with_arguments', 'multiple_tools', 'other_tool', 'non_singleton', 'terminal'])
def test_other_tool_shapes_keep_ordinary_correction(shape):
    with ConversationFixture('openai-compatible') as fx:
        name = 'other' if shape == 'other_tool' else 'lookup'
        fx.add_tool(name, lambda _: None)
        if shape == 'multiple_tools':
            fx.add_tool('other', lambda _: None)
        if shape == 'with_arguments':
            fx.tools[name] = CustomTool(name, 'Synthetic read', {
                'type': 'object', 'properties': {'query': {'type': 'string'}},
                'additionalProperties': False}, lambda _: None, 'v1')
        calls = [wire_call('bad', name, '{"extra":true}')]
        if shape == 'non_singleton':
            calls.append(wire_call('second'))
        if shape == 'terminal':
            # Terminal tools cannot be admitted into a conversation turn.
            fx.tools[name] = CustomTool(name, 'Terminal', {'type': 'object',
                'additionalProperties': False}, lambda _: None, 'v1', terminal=True)
            with pytest.raises(TurnAdmissionError):
                runtime(fx).turn(fx.agent, message='Read', policy=fx.policy)
            assert not fx.requests
            return
        queue_json(fx, calls)
        fx.queue_text('Ordinary fallback')
        turn = runtime(fx).turn(fx.agent, message='Read', policy=fx.policy)
    assert len(fx.requests) == 2 and 'structured_outputs' not in fx.requests[1]
    assert turn.result.exit_code == 0


@pytest.mark.parametrize('enabled, correction, budget, expected', [
    (False, True, 3, 2), (True, False, 3, 1), (True, True, 1, 1),
])
def test_grammar_needs_both_opt_ins_and_budget(enabled, correction, budget, expected):
    with ConversationFixture('openai-compatible') as fx:
        fx.add_tool('lookup', lambda _: None)
        queue_json(fx, [INVALID])
        fx.queue_text('Ordinary fallback')
        runtime(fx, enabled=enabled, correction=correction).turn(
            fx.agent, message='Read', policy=replace(fx.policy, max_provider_requests=budget))
    assert len(fx.requests) == expected
    assert all('structured_outputs' not in request for request in fx.requests)


def test_prepared_turn_refuses_changed_grammar_profile():
    with ConversationFixture('openai-compatible') as fx:
        fx.add_tool('lookup', lambda _: None)
        prepared = runtime(fx).prepare_turn(fx.agent, message='Read', policy=fx.policy)
        with pytest.raises(TurnAdmissionError, match='bindingMismatch'):
            runtime(fx, enabled=False).turn(prepared)
        assert not fx.requests


@pytest.mark.parametrize('changes', [
    {'provider': 'anthropic'}, {'model': 'unsupported-model'},
    {'features': {FEATURE: 'true', 'streaming': False}},
    {'features': {FEATURE: True}}, {'features': {FEATURE: True, 'streaming': True}},
    {'features': {FEATURE: True, 'streaming': False, 'omlx_tool_call_envelope': True}},
])
def test_invalid_profile_combinations(changes):
    from prosaic_runtime import EndpointConfig
    config = dict(base_url='http://127.0.0.1:9/v1', model=MODEL,
                  features={FEATURE: True, 'streaming': False})
    with pytest.raises(ValueError, match=FEATURE):
        EndpointConfig(**{**config, **changes})
