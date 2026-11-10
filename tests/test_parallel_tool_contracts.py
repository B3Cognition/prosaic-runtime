"""Opt-in scheduling declarations preserve the released default-off contract."""
from dataclasses import replace
import json
from pathlib import Path
import pytest
import prosaic_runtime as api
from prosaic_runtime.conversation_preparation import policy_document, policy_from_document

SCHEMA = {'type': 'object', 'properties': {}, 'additionalProperties': False}


def legacy_material():
    ordinary = api.CustomTool('lookup', 'Lookup', SCHEMA, lambda a: {}, 'v1')
    event = api.CustomTool('lookup', 'Lookup', SCHEMA, lambda a: {}, 'v1',
        event_policy=api.ToolEventPolicyV1('1', {}))
    typed = api.CustomTool('lookup', 'Lookup', SCHEMA, lambda a: {}, 'v1',
        result_contract=api.ToolResultContractV1('1', {'type': 'object'}))
    policy = api.RunPolicy(allowed_tools=frozenset({'lookup'}), max_provider_requests=2, max_tool_calls=2)
    runtime = api.ProsaicRuntime(api.RuntimeConfig({'local': api.EndpointConfig(
        'http://127.0.0.1:9/v1', 'synthetic', features={'streaming': False})}, {}, 'local', frozenset({'lookup'})),
        custom_tools={'lookup': ordinary})
    agent = api.ProsaicArtifact('approved', 'subagent', {'tools': ['lookup']}, 'Use supplied facts.')
    binding = api.ToolContractBindingsV1('host', '1', api.InvocationScope('invocation', operation_namespace='tenant'))
    prepared = runtime.prepare_turn(agent, message='Review', policy=policy, input_profile='selected-input-v1',
        tool_contract_bindings=binding)
    return dict(descriptors={k: v.descriptor for k, v in [('ordinary', ordinary), ('eventOnly', event), ('typed', typed)]},
        policy=policy_document(policy), contextIdentity=prepared.context_identity,
        prepared=json.loads(prepared._encoded), frozen=prepared.export_frozen_input().to_dict())


@pytest.mark.parametrize('field,maximum', [('max_concurrency', 16), ('max_buffered_result_bytes', 8388608),
    ('max_pending_events', 2048), ('max_pending_event_bytes', 1048576)])
def test_parallel_policy_bounds_and_closed_codec(field, maximum):
    cls = api.ParallelToolPolicyV1
    for invalid in [0, -1, True, 1.5, float('inf'), maximum + 1]:
        with pytest.raises(ValueError): cls(**{field: invalid})
    assert getattr(cls(**{field: maximum}), field) == maximum
    value = cls().to_dict()
    assert value == dict(version=1, maxConcurrency=4, maxBufferedResultBytes=1048576,
        maxPendingEvents=128, maxPendingEventBytes=65536)
    assert cls.from_dict(value) == cls()
    value['maxConcurrency'] = 2
    assert cls().max_concurrency == 4
    for bad in [dict(cls().to_dict(), extra=1), dict(cls().to_dict(), version=True), {}]:
        with pytest.raises(ValueError): cls.from_dict(bad)


def test_parallel_read_requires_context_and_refuses_terminal():
    for options in [dict(parallel_read=1), dict(parallel_read=True),
                    dict(parallel_read=True, with_context=True, terminal=True)]:
        with pytest.raises(ValueError): api.CustomTool('lookup', 'Lookup', SCHEMA, lambda a: {}, 'v1', **options)
    tool = api.CustomTool('lookup', 'Lookup', SCHEMA, lambda a, c: {}, 'v1', parallel_read=True, with_context=True)
    assert tool.parallel_read is True and tool.descriptor['parallel_read'] is True
    with pytest.raises(ValueError): api.RunPolicy(parallel_tools={})
    bad = api.ParallelToolPolicyV1()
    object.__setattr__(bad, 'max_concurrency', True)
    with pytest.raises(ValueError): api.RunPolicy(parallel_tools=bad)


def test_absent_parallel_fields_preserve_released_bytes():
    fixture = json.loads((Path(__file__).parent/'fixtures/parallel_tools/legacy-identities.json').read_text())
    assert legacy_material() == fixture['material']
    assert 'parallel_tools' not in policy_document(api.RunPolicy())
    tool = api.CustomTool('lookup', 'Lookup', SCHEMA, lambda a: {}, 'v1', parallel_read=False)
    assert 'parallel_read' not in tool.descriptor


def test_enabled_limits_and_tool_declaration_change_identity():
    policy = api.RunPolicy(max_provider_requests=2, max_tool_calls=2, parallel_tools=api.ParallelToolPolicyV1())
    frame = policy_document(policy)
    assert frame['parallel_tools'] == policy.parallel_tools.to_dict()
    assert policy_from_document(frame) == policy
    assert policy_document(replace(policy, parallel_tools=api.ParallelToolPolicyV1(max_concurrency=2))) != frame
    old = api.CustomTool('lookup', 'Lookup', SCHEMA, lambda a, c: {}, 'v1', with_context=True)
    new = api.CustomTool('lookup', 'Lookup', SCHEMA, lambda a, c: {}, 'v1', with_context=True, parallel_read=True)
    assert old.descriptor != new.descriptor
