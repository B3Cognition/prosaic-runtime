"""Independent-review regressions: known effects and aggregate proof bounds."""
from dataclasses import replace
import json,time
import pytest
import prosaic_runtime as api
from test_direct_tool import runtime,POLICY
from support.tool_journal import SqliteJournal


def binding(scope):
    return api.ToolContractBindingsV1('review','1',scope,evidence_authorizer=api.EvidenceAuthorizerV1('sources','1',lambda use:True))


@pytest.mark.parametrize('controlled',[False,True])
def test_completion_sink_cannot_erase_committed_outcome(tmp_path,controlled):
    journal=SqliteJournal(tmp_path/'effects.sqlite');effects=[]
    rt=runtime(lambda args,ctx:effects.append(1) or (api.ToolFailure('not_found') if controlled else {'count':14}),
        with_context=True,operation_key=lambda args,ctx:'key',event_policy=api.ToolEventPolicyV1('1',{}))
    scope=api.InvocationScope('first',operation_namespace='tenant')
    def failed_sink(event):
        if event['event']=='tool_completed':raise OSError('private sink failure')
    result=rt.run_tool('lookup',{'value':1},policy=POLICY,tool_journal=journal,operation_context=scope,
        tool_contract_bindings=binding(scope),on_event=failed_sink)
    assert result.exit_code!=0 and result.metadata['tool_outcome']['code']==('controlled_failure' if controlled else 'ok')
    assert result.metadata['tool_outcome']['effect']=='completed'
    next_scope=api.InvocationScope('second',operation_namespace='tenant')
    rt.run_tool('lookup',{'value':1},policy=POLICY,tool_journal=journal,operation_context=next_scope,tool_contract_bindings=binding(next_scope))
    assert effects==[1]


@pytest.mark.parametrize('size',[64000,65000,65200])
def test_combined_registry_stays_bounded_after_valid_completed_tool(size,tmp_path):
    effects=[];journal=SqliteJournal(tmp_path/'effects.sqlite')
    def handler(args,ctx):
        effects.append(1)
        return api.ToolResultV1(data={'body':'x'*size},evidence=(api.EvidenceDraftV1('source','r1','PRIVATE','/body','Captured'),))
    rt=runtime(handler,with_context=True,operation_key=lambda args,ctx:'key',result_contract=api.ToolResultContractV1('1'))
    for invocation in ('first','second'):
        scope=api.InvocationScope(invocation,operation_namespace='tenant')
        result=rt.run_tool('lookup',{'value':1},policy=POLICY,tool_journal=journal,operation_context=scope,tool_contract_bindings=binding(scope))
        assert result.metadata['tool_outcome']['effect']=='completed'
        evidence=result.metadata['tool_contract_v1']
        assert len(json.dumps(evidence,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode())<=65536
        if size>=65000:assert result.exit_code!=0 and evidence['deliveries']=={}
    assert effects==[1]


def test_delivery_growth_is_checked_before_dispatch():
    from prosaic_runtime.tool_evidence import EvidenceRegistryV1,digest
    scope=api.InvocationScope('bounded');limits=api.ToolContractLimitsV1(max_evidence_bytes=1450)
    registry=EvidenceRegistryV1(replace(binding(scope),limits=limits))
    rt=runtime(lambda args:api.ToolResultV1(data={'body':'x'*300},evidence=(api.EvidenceDraftV1('source','r1','private','/body','Source'),)),result_contract=api.ToolResultContractV1('1'))
    tool=rt._custom_tools['lookup'];outcome={'status':'ok','result':tool.handler({}).to_dict()}
    registry.capture_completion(tool,outcome,call_id='call',call_index=0,arguments_sha256=digest({}),status='completed')
    try:
        public=registry.stage(tool,outcome,outcome,call_id='call',call_index=0,arguments_sha256=digest({}))
        ticket=registry.prepare_delivery({'messages':[public]});registry.begin_delivery(ticket,'request')
        registry.confirm_delivery(ticket,'request')
    except ValueError:pass
    assert len(json.dumps(registry.to_dict(),ensure_ascii=False,sort_keys=True,separators=(',',':')).encode())<=1450


@pytest.mark.parametrize('provider',['openai-compatible','anthropic'])
@pytest.mark.parametrize('surface',['run','turn'])
def test_large_completed_api_result_fails_closed_without_second_request(provider,surface):
    from support.conversation_server import ConversationFixture
    with ConversationFixture(provider) as fx:
        effects=[]
        fx.tools['lookup']=api.CustomTool('lookup','Lookup',{'type':'object','additionalProperties':False},
            lambda args:effects.append(1) or api.ToolResultV1(data={'body':'x'*65000},evidence=(api.EvidenceDraftV1('source','r1','private','/body','Source'),)),
            '1',result_contract=api.ToolResultContractV1('1'))
        fx.queue_calls([{'id':'call','name':'lookup','arguments':'{}'}]);fx.queue_text('Must not be requested')
        scope=api.InvocationScope('api')
        if surface=='turn':
            turn=fx.runtime.turn(fx.agent,message='Review',policy=fx.policy,tool_contract_bindings=binding(scope))
            result=turn.result
            assert turn.evidence.to_dict()['toolOutcomes'][0]['outcome']['result']['data']['body']=='x'*65000
        else:result=fx.runtime.run(fx.agent,policy=fx.policy,tool_contract_bindings=binding(scope))
        assert result.exit_code!=0 and result.stderr=='invalidToolResult' and effects==[1] and len(fx.requests)==1


def test_unrepresentable_registry_budget_refuses_before_any_effect():
    effects=[];rt=runtime(lambda args:effects.append(1) or {'ok':True},event_policy=api.ToolEventPolicyV1('1',{}))
    scope=api.InvocationScope('tiny');small=replace(binding(scope),limits=api.ToolContractLimitsV1(max_evidence_bytes=2))
    with pytest.raises(ValueError):rt.run_tool('lookup',{'value':1},policy=POLICY,tool_contract_bindings=small)
    assert effects==[]


def test_failed_registry_admission_does_not_change_ambient_authority():
    from prosaic_runtime.tool_event_contract import tool_contract_session,current_bindings,current_registry
    from prosaic_runtime.tool_evidence import EvidenceRegistryV1
    outer=binding(api.InvocationScope('outer'));registry=EvidenceRegistryV1(outer)
    bad=replace(binding(api.InvocationScope('bad')),limits=api.ToolContractLimitsV1(max_evidence_bytes=2))
    rt=runtime(event_policy=api.ToolEventPolicyV1('1',{}))
    with tool_contract_session(outer,registry):
        with pytest.raises(ValueError):rt.run_tool('lookup',{'value':1},policy=POLICY,tool_contract_bindings=bad)
        assert current_bindings() is outer and current_registry() is registry
