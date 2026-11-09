import json
import pytest
import prosaic_runtime as api
from support.conversation_server import ConversationFixture
from support.rules_support import runtime_for,tagged


def test_private_fact_change_changes_invocation_not_wire_payload():
    with ConversationFixture('openai-compatible') as fx:
        runtime=runtime_for(fx)
        options=dict(message='Current',input_data={'selected':'x'},input_profile='pack-v1',authoritative_input_profile='facts-v1',policy=fx.policy)
        a=runtime.prepare_turn(tagged('Review.'),authoritative_input_data={'private':'one'},**options)
        b=runtime.prepare_turn(tagged('Review.'),authoritative_input_data={'private':'two'},**options)
        assert a.context_identity!=b.context_identity
        assert a.export_frozen_input().to_dict()['nativePayloadDigest']==b.export_frozen_input().to_dict()['nativePayloadDigest']
        assert a.selected_input_data=={'selected':'x'}
        assert a.input_variant_selection.to_dict()['selectedIndex']==0


def test_default_prepare_identity_stays_v1_or_v2():
    from support.context_counter import fixture_context
    with ConversationFixture('openai-compatible') as fx:
        runtime=runtime_for(fx)
        a=runtime.prepare_turn(tagged('Review.'),message='Current',policy=fx.policy)
        policy,counters=fixture_context(count=lambda request:10)
        b=runtime.prepare_turn(tagged('Review.'),message='Current',policy=fx.policy,context_policy=policy,token_counters=counters)
        assert a.encoding_version=='conversation-turn-v1' and b.encoding_version=='conversation-turn-v2'
        assert a.input_variant_selection is None and b.input_variant_selection is None
        with pytest.raises(api.TurnAdmissionError): a.export_frozen_input()


def test_default_restore_refuses_work_reset_but_acknowledged_attempt_retains_initial_charges():
    from support.context_counter import fixture_context
    policy,counters=fixture_context(count=lambda request:10)
    with ConversationFixture('openai-compatible') as fx:
        runtime=runtime_for(fx); agent=tagged('Review.')
        prepared=runtime.prepare_turn(agent,message='Current',input_profile='pack-v1',policy=fx.policy,context_policy=policy,token_counters=counters)
        frozen=prepared.export_frozen_input().to_dict()
        initial=frozen['initialContextWork']
        frozen['contextWork']={**initial,'candidateChecks':initial['candidateChecks']+1,'candidateBytes':initial['candidateBytes']+200,'checkpointSequence':2}
        kwargs=dict(frozen_input=frozen,policy=fx.policy,limits=api.TurnLimits(),context_policy=policy,token_counters=counters,context_work=api.ContextWorkState.from_dict(initial))
        with pytest.raises(api.TurnAdmissionError): runtime.restore_turn(agent,**kwargs)
        retried=runtime.restore_turn(agent,fresh_attempt=True,**kwargs)
        assert retried.context_work.candidate_checks==initial['candidateChecks']
        assert retried.context_identity==prepared.context_identity
