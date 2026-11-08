import hashlib
import json
import pytest
import prosaic_runtime as api
from support.conversation_server import ConversationFixture
from support.rules_support import runtime_for,tagged


@pytest.mark.parametrize('provider',['openai-compatible','anthropic'])
def test_frozen_restore_keeps_exact_current_message(provider):
    message='  Compare ž with e\u0301\n'
    with ConversationFixture(provider) as fx:
        runtime=runtime_for(fx)
        agent=tagged('ALWAYS use supplied facts. NEVER invent evidence.')
        prepared=runtime.prepare_turn(agent,message=message,input_data={'evidence':'selected'},input_profile='context-pack-input-v1',policy=fx.policy)
        frozen=prepared.export_frozen_input()
        assert frozen.to_dict()['message']==message
        restored=runtime.restore_turn(agent,history=(),frozen_input=frozen,policy=fx.policy,limits=api.TurnLimits())
        assert restored.context_identity==prepared.context_identity
        fx.queue_text('Approved')
        result=runtime.turn(restored)
        payload=json.loads(fx.raw_requests[0])
        content=payload['messages'][-1]['content']
        if type(content) is list: content=''.join(b['text'] for b in content)
        assert json.loads(content)['message']==message
        assert hashlib.sha256(fx.raw_requests[0]).hexdigest()==frozen.to_dict()['nativePayloadDigest']
    assert result.result.exit_code==0


@pytest.mark.parametrize('provider',['openai-compatible','anthropic'])
def test_counted_restore_reserves_work_before_encoding_and_counter(provider):
    from support.context_counter import fixture_context
    calls=[]; checkpoints=[]
    def count(request):
        calls.append(len(checkpoints))
        return 10
    policy,counters=fixture_context(provider,count=count)
    with ConversationFixture(provider) as fx:
        runtime=runtime_for(fx); agent=tagged('Review.')
        prepared=runtime.prepare_turn(agent,message='Current',input_profile='pack-v1',policy=fx.policy,context_policy=policy,token_counters=counters)
        frozen=prepared.export_frozen_input()
        restored=runtime.restore_turn(agent,frozen_input=frozen,policy=fx.policy,limits=api.TurnLimits(),context_policy=policy,token_counters=counters)
        assert calls==[0]
        fx.queue_text('Approved')
        result=runtime.turn(restored,token_counters=counters,context_checkpoint=lambda state:checkpoints.append(state.to_dict()))
        assert result.result.exit_code==0 and len(calls)==2 and calls[1]>0
        assert checkpoints[0]['reservedActiveMicros']>0
        assert hashlib.sha256(fx.raw_requests[0]).hexdigest()==frozen.to_dict()['nativePayloadDigest']


def test_restore_runs_no_counter_guard_probe_or_native_encoding(monkeypatch):
    from support.context_counter import fixture_context
    counts=[];checks=[]
    policy,counters=fixture_context(count=lambda request:counts.append(request) or 10)
    with ConversationFixture('openai-compatible') as fx:
        guard=api.Guard('facts','1',('final',),lambda s,c:checks.append(c) or api.Verdict(True),input_access='authoritativeAndModel')
        runtime=runtime_for(fx,guards=[guard])
        agent=tagged('- ALWAYS check facts. <!-- enforce: facts(stage=final, on_violation=fail) -->')
        prepared=runtime.prepare_turn(agent,message='Current',input_data={'selected':'x'},input_profile='pack-v1',authoritative_input_data={'private':'private fact'},authoritative_input_profile='facts-v1',context_policy=policy,token_counters=counters,policy=fx.policy)
        count=len(counts)
        def forbidden(*a,**k): pytest.fail('restore encoded a native candidate')
        monkeypatch.setattr('prosaic_runtime.conversation_preparation.initial_payload',forbidden)
        restored=runtime.restore_turn(agent,frozen_input=prepared.export_frozen_input(),policy=fx.policy,limits=api.TurnLimits(),context_policy=policy,token_counters=counters)
        assert restored.context_identity==prepared.context_identity
        assert len(counts)==count and checks==[] and fx.raw_requests==[]


@pytest.mark.parametrize('damage',['message','history','input','endpoint','guard','workspace'])
def test_restore_refuses_changed_bindings_without_dispatch(damage):
    from dataclasses import replace
    with ConversationFixture('openai-compatible') as fx:
        runtime=runtime_for(fx)
        agent=tagged('ALWAYS review facts.')
        prepared=runtime.prepare_turn(agent,message='Current',input_data={'selected':'x'},input_profile='pack-v1',policy=fx.policy)
        frozen=prepared.export_frozen_input().to_dict()
        history=(); workspace=None
        if damage=='message': frozen['message']='Different'
        elif damage=='input': frozen['selectedInputData']={'selected':'different'}
        elif damage=='history': history=(api.ConversationMessage.from_dict({'role':'user','turnStatus':'failed','failureCode':'providerFailure','content':[{'type':'text','text':'Foreign'}]}),)
        elif damage=='endpoint':
            config=runtime.config
            profile=config.default_profile
            runtime.config=replace(config,profiles={profile:replace(config.profiles[profile],model='different')})
        elif damage=='guard': agent=tagged('ALWAYS review different facts.')
        else: workspace='/private/tmp'
        with pytest.raises(api.TurnAdmissionError): runtime.restore_turn(agent,history=history,frozen_input=frozen,policy=fx.policy,limits=api.TurnLimits(),workspace=workspace)
        assert not fx.raw_requests


def test_frozen_records_own_values_and_are_closed():
    with ConversationFixture('openai-compatible') as fx:
        frozen=runtime_for(fx).prepare_turn(tagged('Review.'),message='Current',input_data={'selected':['x']},input_profile='pack-v1',policy=fx.policy).export_frozen_input()
    data=frozen.to_dict(); data['selectedInputData']['selected'].clear()
    assert frozen.to_dict()['selectedInputData']=={'selected':['x']}
    with pytest.raises(ValueError): api.FrozenTurnInput.from_dict(frozen.to_dict()|{'foreign':True})


@pytest.mark.parametrize('damage',['variant','counter','private','oversize'])
def test_restore_refuses_variant_counter_private_and_record_drift_before_callbacks(damage):
    from support.context_counter import fixture_context
    counts=[]
    policy,counters=fixture_context(count=lambda request:counts.append(request) or 10)
    with ConversationFixture('openai-compatible') as fx:
        runtime=runtime_for(fx); agent=tagged('Review.')
        prepared=runtime.prepare_turn(agent,message='Current',input_data={'selected':'x'},input_profile='pack-v1',authoritative_input_data={'private':'original'},authoritative_input_profile='facts-v1',policy=fx.policy,context_policy=policy,token_counters=counters)
        frozen=prepared.export_frozen_input().to_dict(); before=len(counts)
        if damage=='variant':
            frozen['inputVariantSelection']['candidateDigests']=['0'*64]
            frozen['inputVariantSelection']['selectedDigest']='0'*64
        elif damage=='private': frozen['authoritativeInputData']['private']='changed'
        elif damage=='counter':
            _,counters=fixture_context(model='changed-model',count=lambda request:pytest.fail('counter executed during restore'))
        else: frozen['selectedInputData']={'oversize':'x'*8388608}
        with pytest.raises(api.TurnAdmissionError): runtime.restore_turn(agent,frozen_input=frozen,policy=fx.policy,limits=api.TurnLimits(),context_policy=policy,token_counters=counters)
        assert len(counts)==before and not fx.raw_requests


@pytest.mark.parametrize('damage',['tool','guard','profile'])
def test_restore_readmits_actual_tool_and_guard_registrations(damage):
    def forbidden(*a,**k): pytest.fail('restoration invoked an execution callback')
    schema={'type':'object','additionalProperties':False}
    with ConversationFixture('openai-compatible') as fx:
        fx.tools['lookup']=api.CustomTool('lookup','Lookup',schema,forbidden,'tool-1')
        guard=api.Guard('facts','guard-1',('final',),forbidden,input_access='model')
        agent=tagged('- ALWAYS check facts. <!-- enforce: facts(stage=final, on_violation=fail) -->',tools=['lookup'])
        runtime=runtime_for(fx,guards=[guard])
        frozen=runtime.prepare_turn(agent,message='Current',input_profile='pack-v1',policy=fx.policy).export_frozen_input().to_dict()
        if damage=='tool': fx.tools['lookup']=api.CustomTool('lookup','Lookup',schema,forbidden,'tool-2')
        elif damage=='guard': guard=api.Guard('facts','guard-2',('final',),forbidden,input_access='model')
        else: frozen['inputProfile']='different'
        with pytest.raises(api.TurnAdmissionError): runtime_for(fx,guards=[guard]).restore_turn(agent,frozen_input=frozen,policy=fx.policy,limits=api.TurnLimits())
        assert not fx.raw_requests
