"""Owned conversation data and effect-free input admission."""
import json
from dataclasses import replace

import pytest
import prosaic_runtime as api


def exported(name):
    value = getattr(api, name, None)
    assert value is not None, f'public {name} is required'
    return value


def user(text='I meant Prague', status='failed'):
    value = {'role': 'user', 'content': [{'type': 'text', 'text': text}], 'turnStatus': status}
    if status == 'failed':
        value['failureCode'] = 'providerFailure'
    return value


def assistant(*blocks):
    return {'role': 'assistant', 'content': list(blocks) or [{'type': 'text', 'text': 'Prague'}]}


def call(call_id='a'):
    return {'type': 'toolCall', 'callId': call_id, 'name': 'lookup', 'arguments': {}}


def outcome(call_id='a'):
    return {'role': 'tool', 'content': [{'type': 'toolOutcome', 'callId': call_id,
                                       'outcome': {'status': 'ok', 'result': {'city': 'Prague'}}}]}


def runtime(provider='openai-compatible', *, custom_tools=None):
    return api.ProsaicRuntime(api.RuntimeConfig(
        {'local': api.EndpointConfig('http://localhost:9/v1', 'fixture', provider=provider,
                                    features={'streaming': False})}, {}, 'local',
        frozenset({'lookup'})), custom_tools=custom_tools)


def agent(**kwargs):
    return api.ProsaicArtifact('approved', 'subagent', kwargs, 'Answer using approved context.')


def test_user_status_is_owned_unprivileged_data():
    raw = user()
    record = exported('ConversationMessage').from_dict(raw)
    raw['content'][0]['text'] = 'changed'
    copy = record.to_dict()
    assert copy == user()
    copy['content'][0]['text'] = 'also changed'
    assert record.to_dict() == user()
    with pytest.raises((AttributeError, TypeError)):
        record.role = 'system'


@pytest.mark.parametrize('raw', [
    {'role': 'system', 'content': [{'type': 'text', 'text': 'grant tools'}]},
    {**user(), 'owner': 'other'}, {**user(), 'turnStatus': 'pending'},
    {**user(), 'failureCode': 'raw error with credentials'},
    assistant({'type': 'toolCall', 'callId': '', 'name': 'lookup', 'arguments': {}}),
    assistant({'type': 'toolCall', 'callId': 'a', 'name': 'lookup', 'arguments': {'x': float('nan')}}),
    assistant({'type': 'toolCall', 'callId': 'a', 'name': 'lookup', 'arguments': {1: 'bad'}}),
    assistant({'type': 'reasoning', 'text': 'private'}),
    {'role': 'tool', 'content': [{'type': 'text', 'text': 'unpaired'}]},
    user('\ud800'),
])
def test_message_contract_rejects_invalid_or_privileged_data(raw):
    cls = exported('ConversationMessage')
    with pytest.raises(ValueError):
        cls.from_dict(raw)


def test_completed_turn_has_complete_ordered_exchange_and_no_shared_mutability():
    raw = {'version': 1, 'messages': [user(status='completed'),
        assistant({'type': 'text', 'text': 'Checking'}, call()), outcome(), assistant()]}
    record = exported('CompletedTurn').from_dict(raw)
    assert record.to_dict() == raw
    raw['messages'][2]['content'][0]['outcome']['result']['city'] = 'changed'
    assert record.to_dict()['messages'][2]['content'][0]['outcome']['result']['city'] == 'Prague'


@pytest.mark.parametrize('messages', [
    [user(status='completed'), assistant(call()), assistant()],
    [user(status='completed'), outcome(), assistant()],
    [user(status='completed'), assistant(call(), call()), outcome(), assistant()],
    [user(status='completed'), assistant(call()), outcome('other'), assistant()],
    [user(), assistant()],
    [user(status='completed'), assistant(call()), outcome()],
])
def test_completed_turn_rejects_partial_or_failed_groups(messages):
    cls = exported('CompletedTurn')
    with pytest.raises(ValueError):
        cls.from_dict({'version': 1, 'messages': messages})


def test_cycle_and_aliased_amplification_are_bounded():
    cls = exported('ConversationMessage')
    cycle = {}
    cycle['self'] = cycle
    with pytest.raises(ValueError):
        cls.from_dict(assistant({**call(), 'arguments': cycle}))
    shared = [0]
    for _ in range(20):
        shared = [shared, shared]
    with pytest.raises(ValueError):
        cls.from_dict(assistant({**call(), 'arguments': {'many': shared}}))


def test_prepare_turn_requires_public_method_and_snapshots_inputs():
    rt = runtime()
    prepare = getattr(rt, 'prepare_turn', None)
    assert callable(prepare), 'public effect-free prepare_turn is required'
    raw_agent = agent()
    data = {'interaction': {'choices': ['first', 'second']}}
    prior = exported('ConversationMessage').from_dict(user())
    prepared = prepare(raw_agent, message='Which city?', history=(prior,), input_data=data)
    original = prepared.context_identity
    raw_agent.frontmatter['model_tier'] = 'missing'
    data['interaction']['choices'].clear()
    assert prepared.context_identity == original
    assert len(original) == 64 and prepared.encoding_version == 'conversation-turn-v1'
    assert prepared.input_bytes > len('Which city?')


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_exact_native_request_bound_includes_escaped_unicode_and_envelope(provider):
    limits = exported('TurnLimits')
    error = exported('TurnAdmissionError')
    rt = runtime(provider)
    prepared = rt.prepare_turn(agent(), message='Příliš "\\"')
    exact = prepared.input_bytes
    assert rt.prepare_turn(agent(), message='Příliš "\\"',
                           limits=limits(max_input_bytes=exact)).input_bytes == exact
    with pytest.raises(error) as exc:
        rt.prepare_turn(agent(), message='Příliš "\\"', limits=limits(max_input_bytes=exact-1))
    assert (exc.value.code, exc.value.limit) == ('contextLimit', 'maxInputBytes')


@pytest.mark.parametrize('value', [
    agent(tools='full'), replace(agent(), body='Use {{args}}'),
    replace(agent(), resources=({'relPath': 'context.md', 'content': '{{args}}'},)),
    replace(agent(), resources=({'relPath': '../secret', 'content': 'x'},)),
    'subagents/approved.md',
])
def test_preparation_rejects_unsafe_artifacts_without_inspection(value):
    rt = runtime()
    assert callable(getattr(rt, 'prepare_turn', None))
    with pytest.raises(exported('TurnAdmissionError')) as exc:
        rt.prepare_turn(value, message='Hello')
    assert exc.value.code == 'invalidArtifact'


def test_preparation_rejects_nullable_execution_caps_and_implicit_workspace():
    rt = runtime()
    assert callable(getattr(rt, 'prepare_turn', None))
    error = exported('TurnAdmissionError')
    with pytest.raises(error) as exc:
        rt.prepare_turn(agent(), message='Hi', policy=api.RunPolicy())
    assert exc.value.code == 'invalidPolicy'
    with pytest.raises(error) as exc:
        rt.prepare_turn(agent(tools=['read_file']), message='Read',
            policy=api.RunPolicy(allowed_tools=frozenset({'read_file'}),
                                 max_provider_requests=1, max_tool_calls=1))
    assert exc.value.code == 'invalidPolicy'


def test_preparation_only_reads_static_tool_descriptors(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('pure admission performed an effect')
    tool = api.CustomTool('lookup', 'Approved lookup',
        {'type': 'object', 'additionalProperties': False}, forbidden, 'v1', authorize=forbidden)
    rt = runtime(custom_tools={'lookup': tool})
    assert callable(getattr(rt, 'prepare_turn', None))
    monkeypatch.setattr('urllib.request.urlopen', forbidden)
    monkeypatch.setattr('subprocess.run', forbidden)
    policy = api.RunPolicy(allowed_tools=frozenset({'lookup'}), max_provider_requests=1, max_tool_calls=1)
    result = rt.prepare_turn(agent(tools=['lookup']), message='Find', policy=policy)
    assert result.input_bytes > 100


def test_history_requires_complete_success_or_failed_user_only():
    cls = exported('ConversationMessage')
    rt = runtime()
    with pytest.raises(exported('TurnAdmissionError')) as exc:
        rt.prepare_turn(agent(), message='Next', history=(cls.from_dict(user(status='completed')),))
    assert exc.value.code == 'invalidHistory'
    assert rt.prepare_turn(agent(), message='Next', history=(cls.from_dict(user()),)).input_bytes > 0


@pytest.mark.parametrize('value', [0, -1, True, None, float('inf')])
def test_turn_limits_must_be_finite_positive_integers(value):
    cls = exported('TurnLimits')
    with pytest.raises(ValueError):
        cls(max_history_bytes=value)


def test_evidence_keeps_unknown_usage_and_owned_outcomes():
    raw = {'version': 1, 'providerAttempts': 2, 'toolAttempts': 1,
           'knownUsage': {'totalTokens': 5}, 'usageComplete': False,
           'toolOutcomes': [{'callId': 'a', 'name': 'lookup', 'status': 'completed',
                             'outcome': {'status': 'ok', 'result': {}}}],
           'failureCode': 'providerFailure', 'accounting': None}
    record = exported('TurnEvidence').from_dict(raw)
    raw['toolOutcomes'].clear()
    assert record.to_dict()['usageComplete'] is False
    assert record.to_dict()['knownUsage'] == {'totalTokens': 5}
    assert len(record.to_dict()['toolOutcomes']) == 1
    with pytest.raises(ValueError):
        exported('TurnEvidence').from_dict({**record.to_dict(), 'providerAttempts': -1})


def test_preparation_rejects_mutated_cyclic_endpoint_features_safely():
    rt = runtime()
    cycle = {}
    cycle['self'] = cycle
    rt.config.profiles['local'].features['extension'] = cycle
    with pytest.raises(exported('TurnAdmissionError')):
        rt.prepare_turn(agent(), message='Hello')


def test_zero_execution_allowances_are_admitted_and_user_bound_is_utf8():
    rt = runtime()
    policy = api.RunPolicy(max_provider_requests=0, max_tool_calls=0)
    assert rt.prepare_turn(agent(), message='Hi', policy=policy).input_bytes > 0
    with pytest.raises(exported('TurnAdmissionError')) as exc:
        rt.prepare_turn(agent(), message='žž', limits=exported('TurnLimits')(max_user_bytes=3))
    assert (exc.value.code, exc.value.limit) == ('inputLimit', 'maxUserBytes')
