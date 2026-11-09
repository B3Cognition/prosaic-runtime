import json
import pytest
import prosaic_runtime as api
from support.conversation_server import ConversationFixture
from support.rules_support import runtime_for, tagged


def rule(name='check_facts',stage='final',action='fail'):
    return tagged(f'- ALWAYS check supplied facts. <!-- enforce: {name}(stage={stage}, on_violation={action}) -->')


def views():
    return dict(input_profile='context-pack-input-v1',input_data={'selected':'visible'},
        authoritative_input_profile='workflow-facts-v1', authoritative_input_data={'private':'PRIVATE-731'})


def test_shared_guard_refuses_separated_inputs_before_http():
    with ConversationFixture('openai-compatible') as fx:
        guard=api.Guard('check_facts','1',('final',),lambda s,c:api.Verdict(True))
        with pytest.raises(api.TurnAdmissionError): runtime_for(fx,guards=[guard]).turn(rule(),message='Review',policy=fx.policy,**views())
        assert fx.raw_requests==[]


def test_model_guard_cannot_access_authoritative_property():
    observed=[]
    def check(stage,context):
        observed.append(context.input_data)
        with pytest.raises(AttributeError): _=context.authoritative_input_data
        return api.Verdict(True)
    with ConversationFixture('openai-compatible') as fx:
        fx.queue_text('Approved')
        guard=api.Guard('check_facts','1',('final',),check,input_access='model')
        result=runtime_for(fx,guards=[guard]).turn(rule(),message='Review',policy=fx.policy,**views())
    assert result.result.exit_code==0 and observed==[{'selected':'visible'}]


@pytest.mark.parametrize('options',[{'fixes':True},{'echo_values':True}])
def test_private_guard_rejects_fix_or_echo_registration(options):
    with pytest.raises(ValueError): api.Guard('check_facts','1',('final',),lambda s,c:api.Verdict(True),input_access='authoritativeAndModel',**options)


def test_private_guard_rejects_retry_binding_before_effects():
    guard=api.Guard('check_facts','1',('final',),lambda s,c:api.Verdict(True),input_access='authoritativeAndModel')
    with ConversationFixture('openai-compatible') as fx:
        with pytest.raises(api.TurnAdmissionError): runtime_for(fx,guards=[guard]).turn(rule(action='retry'),message='Review',policy=fx.policy,**views())
        assert not fx.raw_requests


@pytest.mark.parametrize('provider',['openai-compatible','anthropic'])
def test_private_facts_validate_without_entering_payload(provider):
    observed=[]
    def check(stage,context):
        observed.append((context.input_data,context.authoritative_input_data))
        context.authoritative_input_data['private']='mutated'
        return api.Verdict(True)
    guard=api.Guard('check_facts','1',('final',),check,input_access='authoritativeAndModel')
    with ConversationFixture(provider) as fx:
        fx.queue_text('Approved')
        runtime=runtime_for(fx,guards=[guard])
        prepared=runtime.prepare_turn(rule(),message='Review',policy=fx.policy,**views())
        assert prepared.encoding_version=='conversation-turn-v3'
        result=runtime.turn(prepared)
        assert 'PRIVATE-731' not in b''.join(fx.raw_requests).decode()
    assert result.result.exit_code==0
    assert observed==[({'selected':'visible'},{'private':'mutated'})]
    evidence=result.evidence.to_dict()
    assert evidence['version']==2 and evidence['inputViews']['authoritative']['profile']=='workflow-facts-v1'
    assert evidence['inputViews']['model']['profile']=='context-pack-input-v1'
    assert 'PRIVATE-731' not in json.dumps(evidence)


def test_authoritative_limits_and_fingerprints_do_not_change_legacy_guards():
    from dataclasses import asdict
    assert asdict(api.AuthoritativeInputLimits())=={'max_bytes':2097152,'max_depth':32,'max_nodes':65536}
    with pytest.raises(ValueError): api.AuthoritativeInputLimits(max_bytes=True)
    with ConversationFixture('openai-compatible') as fx:
        shared=runtime_for(fx,guards=[api.Guard('check_facts','1',('final',),lambda s,c:api.Verdict(True))]).prepare_turn(rule(),message='Review',policy=fx.policy)
        model=runtime_for(fx,guards=[api.Guard('check_facts','1',('final',),lambda s,c:api.Verdict(True),input_access='model')]).prepare_turn(rule(),message='Review',policy=fx.policy)
    assert shared.context_identity!=model.context_identity


def test_generic_model_guards_cannot_use_private_ids_or_numbers():
    from prosaic_runtime.guards import args_reference_seen_ids
    guard=args_reference_seen_ids(input_access='model')
    from prosaic_runtime.rule_types import GuardContext
    ctx=GuardContext('before_tool','id','rule','file',{'keys':['recordId']},{},'Review',{'visible':'REC-1'},[],tool_name='lookup',arguments={'recordId':'PRIVATE-731'})
    assert guard.check('before_tool',ctx).ok is False
    from prosaic_runtime.rule_types import ToolResults
    ctx=GuardContext('before_tool','id','rule','file',{'keys':['recordId']},{},'Review',{'visible':'REC-1'},ToolResults([json.dumps({'recordId':'PRIVATE-731'})]),tool_name='lookup',arguments={'recordId':'PRIVATE-731'})
    assert guard.check('before_tool',ctx).ok is True
    assert all(g.input_access=='model' for g in api.generic_guards(input_access='model'))


@pytest.mark.parametrize('observed_tool_result',[False,True])
def test_private_id_only_counts_after_permitted_real_tool_result(observed_tool_result):
    calls=[]
    with ConversationFixture('openai-compatible') as fx:
        def lookup(arguments):
            calls.append(arguments)
            return {'recordId':'PRIVATE-731'}
        fx.tools['lookup']=api.CustomTool('lookup','Lookup',{'type':'object','additionalProperties':False},lookup,'1')
        fx.tools['consume']=api.CustomTool('consume','Consume',{'type':'object','properties':{'recordId':{'type':'string'}},'required':['recordId'],'additionalProperties':False},lambda args:calls.append(args) or {'ok':True},'1')
        if observed_tool_result: fx.queue_calls([{'id':'lookup-1','name':'lookup','arguments':'{}'}])
        fx.queue_calls([{'id':'consume-1','name':'consume','arguments':'{"recordId":"PRIVATE-731"}'}])
        if observed_tool_result: fx.queue_text('Approved')
        agent=tagged('- ALWAYS use seen IDs. <!-- enforce: args_reference_seen_ids(stage=before_tool, on_violation=fail, keys=["recordId"]) -->',tools=fx.tools)
        result=runtime_for(fx,guards=api.generic_guards(input_access='model')).turn(agent,message='Review',policy=fx.policy,**views())
    assert (result.result.exit_code==0) is observed_tool_result
    assert calls==([{}, {'recordId':'PRIVATE-731'}] if observed_tool_result else [])


def test_private_input_is_bounded_before_counter_and_guard_callbacks():
    with ConversationFixture('openai-compatible') as fx:
        options=views()|{'authoritative_input_limits':api.AuthoritativeInputLimits(max_bytes=5)}
        with pytest.raises(api.TurnAdmissionError) as error:
            runtime_for(fx).prepare_turn(tagged('ALWAYS check facts.'),message='Review',policy=fx.policy,**options)
        assert error.value.limit=='authoritativeInputData' and not fx.raw_requests


def test_authoritative_policy_rejects_false_and_valid_guard_versions_allow_spaces():
    with ConversationFixture('openai-compatible') as fx:
        with pytest.raises(api.TurnAdmissionError):
            runtime_for(fx).prepare_turn(tagged('Review.'),message='Review',policy=fx.policy,
                **(views()|{'authoritative_input_limits':False}))
        guard=api.Guard('check_facts','review build 1',('final',),lambda s,c:api.Verdict(True),input_access='authoritativeAndModel')
        fx.queue_text('Approved')
        result=runtime_for(fx,guards=[guard]).turn(rule(),message='Review',policy=fx.policy,**views())
    assert result.result.exit_code==0
