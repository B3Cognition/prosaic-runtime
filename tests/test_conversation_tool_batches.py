"""Malformed model batches must fail before any registered effect boundary."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest
from prosaic_runtime import CustomTool, InvocationScope, ProsaicArtifact, ProsaicRuntime
from support.conversation_server import turn_fixture


def wire_call(call_id='a', name='lookup', arguments='{}'):
    return {'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': arguments}}


INVALID = [
    wire_call('b', 'not_granted'), wire_call('', 'lookup'),
    {'type': 'function', 'function': {'name': 'lookup', 'arguments': '{}'}},
    {**wire_call('b'), 'extra': True}, {**wire_call('b'), 'type': 'other'},
    {'id': 'b', 'type': 'function', 'function': {'name': 'lookup', 'arguments': '{}', 'extra': True}},
    wire_call('b', arguments={'not': 'a string'}), wire_call('b', arguments='{"x":1,"x":2}'),
    wire_call('b', arguments='{"x":NaN}'), wire_call('b', arguments='{"x":1}'),
    wire_call('a'), None,
]


def queue_json(fx, calls):
    if fx.provider == 'anthropic':
        blocks = []
        for call in calls:
            if type(call) is not dict:
                blocks.append(call)
                continue
            function = call.get('function', {})
            raw = function.get('arguments')
            arguments = json.loads(raw) if type(raw) is str else raw
            block = {'type': 'tool_use', 'name': function.get('name'), 'input': arguments}
            if 'id' in call:
                block['id'] = call['id']
            if 'extra' in call or 'extra' in function:
                block['extra'] = True
            if call.get('type') != 'function':
                block['type'] = call.get('type')
            blocks.append(block)
        fx.queue({'type': 'message', 'id': 'native', 'model': 'fixture-model', 'role': 'assistant',
                  'content': blocks, 'stop_reason': 'tool_use',
                  'usage': {'input_tokens': 2, 'output_tokens': 3}})
    else:
        fx.queue({'choices': [{'message': {'role': 'assistant', 'content': '', 'tool_calls': calls},
                              'finish_reason': 'tool_calls'}],
                  'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5}})


def queue_stream(fx, calls):
    fx.streaming = True
    events = []
    if fx.provider == 'anthropic':
        def event(kind, **data):
            value = {'type': kind, **data}
            events.append(f'event: {kind}\ndata: {json.dumps(value)}\n\n')
        event('message_start', message={'type': 'message', 'id': 'native', 'model': 'fixture-model',
              'role': 'assistant', 'content': [], 'usage': {'input_tokens': 2, 'output_tokens': 0}})
        for index, call in enumerate(calls):
            if type(call) is not dict:
                event('content_block_start', index=index, content_block=call)
                continue
            function = call.get('function', {})
            block = {'type': 'tool_use', 'name': function.get('name'), 'input': {}}
            if 'id' in call:
                block['id'] = call['id']
            if 'extra' in call or 'extra' in function:
                block['extra'] = True
            if call.get('type') != 'function':
                block['type'] = call.get('type')
            event('content_block_start', index=index, content_block=block)
            raw = function.get('arguments')
            if type(raw) is str:
                for fragment in (raw[:len(raw)//2], raw[len(raw)//2:]):
                    event('content_block_delta', index=index,
                          delta={'type': 'input_json_delta', 'partial_json': fragment})
            else:
                event('content_block_delta', index=index,
                      delta={'type': 'input_json_delta', 'partial_json': raw})
            event('content_block_stop', index=index)
        event('message_delta', delta={'stop_reason': 'tool_use'}, usage={'output_tokens': 3})
        event('message_stop')
    else:
        for index, call in enumerate(calls):
            if type(call) is not dict:
                fragments = [call]
            else:
                initial = {**call, 'index': index}
                function = call.get('function')
                raw = function.get('arguments') if type(function) is dict else None
                if type(raw) is str:
                    initial['function'] = {**function, 'arguments': raw[:len(raw)//2]}
                    fragments = [initial, {'index': index, 'function': {'arguments': raw[len(raw)//2:]}}]
                else:
                    fragments = [initial]
            for fragment in fragments:
                value = {'choices': [{'index': 0, 'delta': {'tool_calls': [fragment]}, 'finish_reason': None}]}
                events.append(f'data: {json.dumps(value)}\n\n')
        final = {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls'}],
                 'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5}}
        events.extend([f'data: {json.dumps(final)}\n\n', 'data: [DONE]\n\n'])
    fx.responses.append((200, 'text/event-stream', ''.join(events).encode()))


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('invalid', INVALID)
def test_invalid_second_call_prevents_first_effect_and_authorization(turn_fixture, provider, streaming, invalid):
    with turn_fixture(provider) as fx:
        effects, authorizations, events = [], [], []
        fx.add_tool('lookup', lambda args: effects.append(args) or {},
                    authorize=lambda args: authorizations.append(args) or True)
        calls = [wire_call(), deepcopy(invalid)]
        (queue_stream if streaming else queue_json)(fx, calls)
        fx.queue_text('Should not be reached')
        result = fx.runtime.turn(fx.agent, message='Search', policy=fx.policy, on_event=events.append)
        assert effects == [] and authorizations == []
        assert result.result.metadata['failure_reason'] == 'invalidToolRequest'
        assert result.evidence.to_dict()['toolAttempts'] == 0
        assert not any(event['event'] == 'tool_started' for event in events)
        assert len(fx.requests) == 1 and result.completed_turn is None


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
@pytest.mark.parametrize('streaming', [False, True])
def test_tool_free_interpretation_cannot_dispatch_registered_handler(turn_fixture, provider, streaming):
    with turn_fixture(provider) as fx:
        effects, authorizations = [], []
        fx.add_tool('lookup', lambda args: effects.append(args) or {},
                    authorize=lambda args: authorizations.append(args) or True)
        (queue_stream if streaming else queue_json)(fx, [wire_call()])
        fx.queue_text('Should not be reached')
        agent = replace(fx.agent, frontmatter={})
        result = fx.runtime.turn(agent, message='"yes, approve"',
                                 policy=replace(fx.policy, max_tool_calls=0))
        assert effects == [] and authorizations == []
        assert result.result.metadata['failure_reason'] == 'invalidToolRequest'
        assert result.evidence.to_dict()['toolAttempts'] == 0


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_valid_batch_obeys_sequential_tool_allowance(turn_fixture, provider):
    with turn_fixture(provider) as fx:
        effects = []
        fx.add_tool('lookup', lambda args: effects.append(args) or {})
        queue_json(fx, [wire_call('a'), wire_call('b')])
        result = fx.runtime.turn(fx.agent, message='Search', policy=replace(fx.policy, max_tool_calls=1))
        assert effects == [{}]
        assert result.result.metadata['failure_reason'] == 'budgetExceeded'


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_duplicate_provider_envelope_keys_cannot_hide_tool_requests(turn_fixture, provider):
    with turn_fixture(provider) as fx:
        fx.add_tool('lookup', lambda args: pytest.fail('invalid envelope dispatched'))
        raw = ('{"choices":[],"choices":[]}' if provider == 'openai-compatible' else
               '{"type":"message","type":"message"}')
        fx.responses.append((200, 'application/json', raw.encode()))
        result = fx.runtime.turn(fx.agent, message='Search', policy=fx.policy)
        assert result.result.metadata['failure_reason'] == 'invalidToolRequest'


def test_explicit_workspace_and_scopes_are_required_before_provider_dispatch(turn_fixture):
    with turn_fixture('openai-compatible') as fx:
        from prosaic_runtime import RunPolicy, TurnAdmissionError
        agent = ProsaicArtifact('approved', 'subagent', {'tools': ['read_file']}, 'Read approved data.')
        rt = ProsaicRuntime(replace(fx.runtime.config, allowed_tools=frozenset({'read_file'})))
        policy = RunPolicy(allowed_tools=frozenset({'read_file'}), max_provider_requests=1, max_tool_calls=1)
        with pytest.raises(TurnAdmissionError):
            rt.turn(agent, message='Read', policy=policy)
        with pytest.raises(TurnAdmissionError):
            rt.turn(agent, message='Read', policy=policy, workspace='/private/tmp')
        assert fx.requests == []


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_zero_tool_binding_never_enters_journal_or_operation_resolver(turn_fixture, provider):
    with turn_fixture(provider) as fx:
        def forbidden(*args):
            pytest.fail('tool-free interpretation crossed an effect boundary')
        class Journal:
            identity = 'fixture-journal'
            contract_version = 'tool-journal-v1'
            claim = staticmethod(forbidden)
            commit = staticmethod(forbidden)
        fx.add_tool('lookup', forbidden, authorize=forbidden, operation_key=forbidden)
        queue_json(fx, [wire_call()])
        result = fx.runtime.turn(replace(fx.agent, frontmatter={}), message='"yes, approve"',
            policy=replace(fx.policy, max_tool_calls=0),
            operation_context=InvocationScope('interpret', operation_namespace='fixture-ops'),
            tool_journal=Journal())
        assert result.result.metadata['failure_reason'] == 'invalidToolRequest'
        assert result.evidence.to_dict()['toolAttempts'] == 0


@pytest.mark.parametrize('streaming', [False, True])
def test_native_duplicate_argument_keys_do_not_normalize_into_valid_data(turn_fixture, streaming):
    with turn_fixture('anthropic') as fx:
        effects = []
        fx.tools['lookup'] = CustomTool('lookup', 'Lookup',
            {'type': 'object', 'properties': {'query': {'type': 'string'}}, 'additionalProperties': False},
            lambda args: effects.append(args) or {}, 'v1')
        if streaming:
            queue_stream(fx, [wire_call(arguments='{"query":"one","query":"two"}')])
        else:
            queue_json(fx, [wire_call()])
            status, content_type, body = fx.responses.pop()
            body = body.replace(b'"input": {}', b'"input": {"query":"one","query":"two"}')
            fx.responses.append((status, content_type, body))
        result = fx.runtime.turn(fx.agent, message='Find', policy=fx.policy)
        assert effects == [] and result.result.metadata['failure_reason'] == 'invalidToolRequest'


def test_mutated_cyclic_policy_is_rejected_as_safe_admission_error(turn_fixture):
    from prosaic_runtime import TurnAdmissionError
    with turn_fixture('openai-compatible') as fx:
        cycle = []
        cycle.append(cycle)
        malformed = replace(fx.policy, read_roots=cycle)
        with pytest.raises(TurnAdmissionError) as exc:
            fx.runtime.prepare_turn(fx.agent, message='Hi', policy=malformed)
        assert exc.value.code == 'invalidPolicy' and fx.requests == []
