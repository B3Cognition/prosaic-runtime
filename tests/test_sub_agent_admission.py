"""A delegate is a sealed, opt-in tool on the role-aware API surface."""
from dataclasses import replace

import pytest

import prosaic_runtime as sdk
from support.conversation_server import ConversationFixture


def agent(tools=()):
    return sdk.ProsaicArtifact("approved", "subagent", {"tools": list(tools)},
                               "ALWAYS use approved context. NEVER write files.")


def bind(runtime):
    scope = sdk.InvocationScope('parent', operation_namespace='delegation')
    contracts = sdk.ToolContractBindingsV1('delegation', 'v1', scope,
        evidence_authorizer=sdk.EvidenceAuthorizerV1('evidence', 'v1', lambda _: True))
    policy = sdk.RunPolicy(
        allowed_tools=frozenset({'review'}), max_provider_requests=3,
        max_tool_calls=1, max_reported_tokens=100,
    )
    child = sdk.SubAgentSpecV1(
        'review', 'Review a finding', agent(), 'review-v1',
        sdk.RunPolicy(max_provider_requests=1, max_tool_calls=0, max_reported_tokens=40),
        {'type': 'object', 'properties': {'finding': {'type': 'string'}},
         'required': ['finding'], 'additionalProperties': False},
    )
    frame = sdk.SubAgentFrameV1(
        scope, sdk.SubAgentPolicyV1(1, 3, 1, 100), 'grants-v1',
        {'review': {'context': {'facts': ['approved']}, 'sources': ['fact-v1']}},
    )
    parent = agent(('review',))
    runner = sdk.ProsaicRuntime(
        replace(runtime.config, allowed_tools=policy.allowed_tools),
        custom_tools=sdk.bind_sub_agent_tools((child,)),
    )
    return runner, parent, policy, contracts, frame


def test_selected_delegate_round_trips_frozen_frame():
    assert hasattr(sdk, 'SubAgentSpecV1'), 'delegate admission capability is missing'
    with ConversationFixture('openai-compatible') as fixture:
        runner, parent, policy, contracts, frame = bind(fixture.runtime)
        original = runner.prepare_turn(parent, message='Review', policy=policy,
            tool_contract_bindings=contracts, sub_agent_frame=frame)
        frozen = original.export_frozen_input().to_dict()
        assert (original.encoding_version, frozen['version']) == ('conversation-turn-v6', 5)
        assert frozen['bindings']['subAgentsV1'] == frame.to_dict()
        restored = runner.restore_turn(parent, frozen_input=frozen, policy=policy,
            limits=sdk.TurnLimits(), tool_contract_bindings=contracts, sub_agent_frame=frame)
        assert original.context_identity == restored.context_identity


def test_delegate_refused_outside_api_turn():
    with ConversationFixture('openai-compatible') as fixture:
        runner, parent, policy, _, _ = bind(fixture.runtime)
        result = runner.run_tool('review', {'task': 'Check'}, policy=policy)
        assert result.metadata['failure_reason'] == 'unsupported_direct_tool'
        with pytest.raises(ValueError, match="admitted API turn"):
            runner.run(parent, policy=policy)
        assert not fixture.requests


def test_unselected_registration_keeps_legacy_bytes():
    with ConversationFixture('openai-compatible') as fixture:
        runner, _, _, _, _ = bind(fixture.runtime)
        parent = agent()
        original = fixture.runtime.prepare_turn(parent, message='Review')
        prepared = runner.prepare_turn(parent, message='Review')
        assert prepared.encoding_version == original.encoding_version
        assert prepared.context_identity == original.context_identity
