import json
import time
import pytest
import prosaic_runtime as api
from support.conversation_server import ConversationFixture
from support.rules_support import runtime_for,tagged
from support.context_counter import fixture_context


def test_all_variants_share_work_state():
    seen=[]
    def count(request):
        seen.append(len(json.dumps(request,allow_nan=False).encode('utf-8')))
        return 900 if 'rich' in json.dumps(request) else 20
    policy,counters=fixture_context(count=count,capacity=300,reserve=100)
    with ConversationFixture('openai-compatible') as fx:
        p=runtime_for(fx).prepare_turn_variants(tagged('Review.'),message='Current',input_data_variants=[{'rich':2},{'rich':1},{'base':0}],policy=fx.policy,context_policy=policy,token_counters=counters)
    assert p.context_work.candidate_checks==3
    assert p.context_work.candidate_bytes==sum(seen)
    assert p.context_work.readmission_pending is True
    assert p.context_work.readmission_candidate_bytes==p.input_bytes


def test_unfunded_optional_trial_keeps_verified_base():
    calls=[]
    policy,counters=fixture_context(checks=2,count=lambda request:calls.append(request) or 10)
    with ConversationFixture('openai-compatible') as fx:
        p=runtime_for(fx).prepare_turn_variants(tagged('Review.'),message='Current',input_data_variants=[{'optional':'rich'},{'base':'required'}],policy=fx.policy,context_policy=policy,token_counters=counters)
    assert len(calls)==1 and p.selected_input_data=={'base':'required'}
    assert p.input_variant_selection.to_dict()['selectionWorkLimited'] is True
    assert p.context_work.candidate_checks==1


def test_running_counter_timeout_fails_without_dispatch():
    def count(request):
        if 'optional' in json.dumps(request): time.sleep(0.04)
        return 10
    policy,counters=fixture_context(timeout_ms=20,count=count)
    with ConversationFixture('openai-compatible') as fx:
        with pytest.raises(api.TurnAdmissionError) as error:
            runtime_for(fx).prepare_turn_variants(tagged('Review.'),message='Current',input_data_variants=[{'optional':'rich'},{'base':'required'}],policy=fx.policy,context_policy=policy,token_counters=counters)
        assert error.value.code=='contextLimit' and error.value.limit=='assemblyTimeoutMs'
        assert not fx.raw_requests


def test_unfunded_optional_bytes_keep_final_base_readmission_funded():
    from dataclasses import replace
    calls=[]
    policy,counters=fixture_context(count=lambda request:calls.append(request) or 10)
    with ConversationFixture('openai-compatible') as fx:
        runtime=runtime_for(fx); agent=tagged('Review.')
        base=runtime.prepare_turn(agent,message='Current',input_data={'base':'required'},input_profile='pack-v1',policy=fx.policy,context_policy=policy,token_counters=counters)
        calls.clear()
        policy=replace(policy,max_assembly_bytes=base.input_bytes*2)
        p=runtime.prepare_turn_variants(agent,message='Current',input_data_variants=[{'optional':'x'*100},{'base':'required'}],input_profile='pack-v1',policy=fx.policy,context_policy=policy,token_counters=counters)
        assert len(calls)==1 and p.input_variant_selection.to_dict()['selectionWorkLimited'] is True
        assert p.context_work.candidate_bytes+p.context_work.readmission_candidate_bytes==policy.max_assembly_bytes


def test_interrupted_reservation_cannot_restart_variant_work():
    policy,counters=fixture_context(count=lambda request:10)
    with ConversationFixture('openai-compatible') as fx:
        runtime=runtime_for(fx)
        p=runtime.prepare_turn_variants(tagged('Review.'),message='Current',input_data_variants=[{'optional':'x'},{'base':'required'}],policy=fx.policy,context_policy=policy,token_counters=counters)
        exhausted=api.ContextWorkState.from_dict({**p.context_work.to_dict(),'reservedActiveMicros':policy.assembly_timeout_ms*1000})
        with pytest.raises(api.TurnAdmissionError) as error: runtime.turn(p,token_counters=counters,context_work=exhausted)
        assert error.value.code=='contextLimit' and not fx.raw_requests
