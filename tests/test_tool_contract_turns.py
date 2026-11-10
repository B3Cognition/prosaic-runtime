from dataclasses import replace
import json
import pytest
import prosaic_runtime as api
from support.conversation_server import ConversationFixture
from test_tool_evidence import bindings
from test_tool_evidence_delivery import configure
from test_citation_execution import dynamic_answer


def test_disabled_prepared_input_and_evidence_are_identical():
    with ConversationFixture('openai-compatible') as fx:
        before = fx.runtime.prepare_turn(fx.agent, message='Review', policy=fx.policy)
        after = fx.runtime.prepare_turn(fx.agent, message='Review', policy=fx.policy, tool_contract_bindings=None)
        assert before._encoded == after._encoded and before.context_identity == after.context_identity
        assert before.encoding_version == 'conversation-turn-v1' and 'toolContractV1' not in json.loads(before._encoded)


@pytest.mark.parametrize('profile', [None, 'selected-input-v1'])
def test_enabled_binding_changes_identity_and_roundtrips_optional_views(profile):
    with ConversationFixture('openai-compatible') as fx:
        runtime = fx.runtime
        first = runtime.prepare_turn(fx.agent, message='Review', policy=fx.policy, input_profile=profile,
            tool_contract_bindings=bindings())
        second = runtime.prepare_turn(fx.agent, message='Review', policy=fx.policy, input_profile=profile,
            tool_contract_bindings=replace(bindings(), version='2'))
        assert first.encoding_version == 'conversation-turn-v4' and first.context_identity != second.context_identity
        frozen = first.export_frozen_input()
        assert frozen.to_dict()['encodingVersion'] == 'conversation-turn-v4'
        restored = runtime.restore_turn(fx.agent, frozen_input=frozen, policy=fx.policy, limits=api.TurnLimits(),
            tool_contract_bindings=bindings())
        assert restored.context_identity == first.context_identity
        assert ('inputViews' in json.loads(first._encoded)) is (profile is not None)
        with pytest.raises(api.TurnAdmissionError):
            runtime.restore_turn(fx.agent, frozen_input=frozen, policy=fx.policy, limits=api.TurnLimits())


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_saved_answer_sidecar_matches_frozen_projection(provider):
    with ConversationFixture(provider) as fx:
        calls = []
        binding = replace(configure(fx, calls), citations=api.CitationPolicyV1('required'))
        dynamic_answer(fx)
        runtime = fx.runtime
        prepared = runtime.prepare_turn(fx.agent, message='Review', policy=fx.policy, tool_contract_bindings=binding)
        turn = runtime.turn(prepared, tool_contract_bindings=binding)
        assert turn.result.exit_code == 0 and calls == [1]
        document = turn.evidence.to_dict()
        assert document['version'] == 3 and 'toolContractV1' in document
        sidecar = document['toolContractV1']['citations']
        assert api.CitationsV1.from_dict(sidecar).verify_answer(turn.result.stdout)
        assert api.TurnEvidence.from_dict(document).to_dict() == document
        record = document['toolContractV1']['evidence']['records'][0]
        record['contentSha256'] = '0' * 64
        with pytest.raises(ValueError): api.TurnEvidence.from_dict(document)


def test_finite_attempt_caps_and_reservations_before_spend():
    with ConversationFixture('openai-compatible') as fx:
        for policy, limits in [(replace(fx.policy, max_provider_requests=None), api.TurnLimits()),
                (fx.policy, api.TurnLimits(max_evidence_bytes=1000)),
                (fx.policy, api.TurnLimits(max_event_bytes=128))]:
            with pytest.raises(api.TurnAdmissionError):
                fx.runtime.turn(fx.agent, message='Review', policy=policy, limits=limits, tool_contract_bindings=bindings())
        assert not fx.requests


def test_new_marker_never_fits_legacy_evidence_shape():
    old = {'version': 1, 'providerAttempts': 0, 'toolAttempts': 0, 'knownUsage': {}, 'usageComplete': True,
        'toolOutcomes': [], 'failureCode': None, 'accounting': None}
    assert api.TurnEvidence.from_dict(old).to_dict() == old
    with pytest.raises(ValueError): api.TurnEvidence.from_dict({**old, 'toolContractV1': {}})
    with pytest.raises(ValueError): api.TurnEvidence.from_dict({**old, 'version': 3})
