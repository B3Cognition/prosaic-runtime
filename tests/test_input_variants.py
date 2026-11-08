import hashlib
import json
import pytest
import prosaic_runtime as api
from support.conversation_server import ConversationFixture
from support.rules_support import runtime_for,tagged
from support.context_counter import fixture_context


def history():
    result=[]
    for text in ('EARLY','RECENT'):
        result.extend([api.ConversationMessage.from_dict({'role':'user','turnStatus':'completed','content':[{'type':'text','text':text}]}),api.ConversationMessage.from_dict({'role':'assistant','content':[{'type':'text','text':'Accepted'}]})])
    return tuple(result)


@pytest.mark.parametrize('provider',['openai-compatible','anthropic'])
def test_optional_evidence_cannot_evict_more_history(provider):
    observed=[]
    def count(request):
        text=json.dumps(request); observed.append(text)
        if 'EARLY' in text or ('optional' in text and 'RECENT' in text): return 500
        return 50
    context,counters=fixture_context(provider,mode='omitOldestTurns',capacity=300,reserve=100,count=count)
    with ConversationFixture(provider) as fx:
        runtime=runtime_for(fx)
        p=runtime.prepare_turn_variants(tagged('Review.'),message='Current',history=history(),input_data_variants=[{'optional':'evidence'},{'task':'required'}],policy=fx.policy,context_policy=context,token_counters=counters)
        assert p.input_variant_selection.to_dict()['selectedIndex']==1
        assert p.context_report.retained_turn_count==1
        assert len(observed)==3 and 'RECENT' in observed[-1] and 'EARLY' not in observed[-1]
        frozen=p.export_frozen_input()
        fx.queue_text('Approved')
        result=runtime.turn(p,token_counters=counters)
        assert result.result.exit_code==0
        assert 'RECENT' in fx.raw_requests[0].decode() and 'optional' not in fx.raw_requests[0].decode()
        assert hashlib.sha256(fx.raw_requests[0]).hexdigest()==frozen.to_dict()['nativePayloadDigest']


@pytest.mark.parametrize('provider',['openai-compatible','anthropic'])
def test_nonadditive_counter_checks_actual_candidates(provider):
    requests=[]
    def count(request):
        text=json.dumps(request); requests.append(text)
        if 'too-rich' in text: return 900
        if 'medium' in text: return 70
        return 90
    context,counters=fixture_context(provider,capacity=300,reserve=100,count=count)
    with ConversationFixture(provider) as fx:
        runtime=runtime_for(fx);agent=tagged('Review.')
        variants=[{'choice':'too-rich'},{'choice':'medium'},{'choice':'base'}]
        p=runtime.prepare_turn_variants(agent,message='Current',input_data_variants=variants,policy=fx.policy,context_policy=context,token_counters=counters,input_profile='pack-v1')
        selection=p.input_variant_selection.to_dict()
        assert selection['selectedIndex']==1 and p.selected_input_data=={'choice':'medium'}
        assert p.context_report.input_tokens==70 and len(requests)==3
        assert p.context_work.candidate_checks==3
        frozen=p.export_frozen_input(); before=len(requests)
        restored=runtime.restore_turn(agent,frozen_input=frozen,history=(),policy=fx.policy,limits=api.TurnLimits(),context_policy=context,token_counters=counters)
        assert len(requests)==before and restored.input_variant_selection.to_dict()==selection


def test_variant_admission_bounds_and_owns_all_candidates_before_counter():
    calls=[]
    context,counters=fixture_context(count=lambda request:calls.append(request) or 10)
    with ConversationFixture('openai-compatible') as fx:
        runtime=runtime_for(fx); agent=tagged('Review.')
        for variants in ([],[{}]*9,[{'invalid':float('nan')},{}],[{'invalid':'\ud800'},{}]):
            with pytest.raises(api.TurnAdmissionError): runtime.prepare_turn_variants(agent,message='Current',input_data_variants=variants,policy=fx.policy,context_policy=context,token_counters=counters)
        assert calls==[] and not fx.raw_requests
        variants=[{'facts':['x']},{'task':'base'}]
        p=runtime.prepare_turn_variants(agent,message='Current',input_data_variants=variants,policy=fx.policy,context_policy=context,token_counters=counters)
        variants[0]['facts'].clear()
        assert p.selected_input_data=={'facts':['x']}


@pytest.mark.parametrize('provider',['openai-compatible','anthropic'])
@pytest.mark.parametrize('kind',['exact','conservativeUpperBound'])
def test_tools_framing_unicode_and_output_reservation_are_counted(provider,kind):
    from dataclasses import replace
    requests=[]
    policy,counters=fixture_context(provider,count=lambda request:requests.append(request) or 20,reserve=123)
    entry=counters.resolve(policy.counter_identity)
    counters=api.TokenCounterRegistry({policy.counter_identity:api.TokenCounterRegistration(replace(entry.descriptor,counting_kind=kind),entry.counter)})
    with ConversationFixture(provider) as fx:
        fx.tools['lookup']=api.CustomTool('lookup','Synthetic lookup framing',{'type':'object','properties':{'query':{'type':'string'}},'additionalProperties':False},lambda args:{},'1')
        runtime=runtime_for(fx)
        p=runtime.prepare_turn_variants(tagged('Review.',tools=['lookup']),message='ž "\\"',input_data_variants=[{'optional':'e\u0301\n"\\"'},{'base':'required'}],policy=fx.policy,context_policy=policy,token_counters=counters)
        assert p.context_report.counting_kind==kind and p.input_variant_selection.to_dict()['selectedIndex']==0
        assert all(request['tools'] and request['max_tokens']==123 for request in requests)
        fx.queue_text('Approved')
        result=runtime.turn(p,token_counters=counters)
        assert result.result.exit_code==0
        assert hashlib.sha256(fx.raw_requests[0]).hexdigest()==p.export_frozen_input().to_dict()['nativePayloadDigest']
