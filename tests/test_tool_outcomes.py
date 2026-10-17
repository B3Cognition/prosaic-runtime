from copy import deepcopy
from dataclasses import FrozenInstanceError
import pytest
from prosaic_runtime import CustomTool,ToolFailure,InvocationScope,ToolClaim
from prosaic_runtime.tools import execute_custom_tool
from prosaic_runtime.tool_effects import ToolEffectFailure

SCHEMA={'type':'object','properties':{'id':{'type':'string'}},'required':['id'],'additionalProperties':False}

def test_public_outcome_is_strict_owned_and_immutable():
    from prosaic_runtime import ToolOutcome
    o=ToolOutcome('invalid_arguments')
    assert o.to_dict()==dict(version=1,code='invalid_arguments',phase='admission',effect='not_started',
        recovery='correct_arguments',replayed=False)
    d=o.to_dict();d['code']='ok'
    assert o.code=='invalid_arguments'
    with pytest.raises(FrozenInstanceError):o.code='ok'
    assert ToolOutcome.from_dict(o.to_dict())==o

@pytest.mark.parametrize('change',[
    {'version':True},{'version':2},{'code':'PRIVATE exception text'},{'phase':'execution'},
    {'effect':'completed'},{'recovery':'none'},{'replayed':True},{'replayed':0},{'extra':'secret'}])
def test_inconsistent_or_unknown_outcome_cannot_authorize_recovery(change):
    from prosaic_runtime import ToolOutcome
    d=ToolOutcome('invalid_arguments').to_dict();d.update(change)
    with pytest.raises(ValueError):ToolOutcome.from_dict(d)

def run(handler=lambda args:{'value':1},raw='{"id":"ok"}',**kwargs):
    outcomes=[];called=[]
    def callback(args):called.append(1);return handler(args)
    tool=CustomTool('lookup','Lookup',SCHEMA,callback,'v1',**kwargs)
    result=execute_custom_tool(tool,raw,check_boundary=lambda:None,on_outcome=outcomes.append)
    return result,[o.to_dict() for o in outcomes],called

def test_pre_dispatch_argument_failure_has_no_handler_effect():
    result,events,called=run(raw='{"id":1}')
    assert result==dict(status='error',error='invalid_arguments') and called==[]
    assert len(events)==1 and events[0]['code']=='invalid_arguments' and events[0]['effect']=='not_started'

def test_denial_never_becomes_repair_permission():
    result,events,called=run(authorize=lambda args:False)
    assert result==dict(status='error',error='authorization_denied') and called==[]
    assert events[0]['recovery']=='stop'

def test_controlled_failure_code_cannot_impersonate_runtime_admission():
    result,events,called=run(handler=lambda args:ToolFailure('invalid_arguments'))
    assert result==dict(status='error',error='invalid_arguments') and called==[1]
    assert events[0]['code']=='controlled_failure' and events[0]['effect']=='completed'
    assert events[0]['recovery']=='stop'

def test_ordinary_error_dictionary_is_data_not_recovery_authority():
    value={'status':'error','error':'invalid_arguments','tool_outcome':{'recovery':'correct_arguments'}}
    result,events,called=run(handler=lambda args:value)
    assert result==dict(status='ok',result=value) and called==[1]
    assert events[0]['code']=='ok' and events[0]['recovery']=='none'

@pytest.mark.parametrize('kind,code',[('raise','handler_error'),('invalid','invalid_result'),('large','result_limit')])
def test_post_dispatch_failures_remain_uncertain_without_private_content(kind,code):
    def handler(args):
        if kind=='raise':raise RuntimeError('PRIVATE-CALLBACK-SECRET')
        if kind=='invalid':return object()
        return 'PRIVATE-CALLBACK-SECRET'*100
    result,events,called=run(handler=handler,max_result_bytes=100)
    assert result['status']=='error' and called==[1]
    assert events[0]['code']==code and events[0]['effect']=='unknown' and events[0]['recovery']=='reconcile'
    assert 'PRIVATE-CALLBACK-SECRET' not in str(result)+str(events)

class Journal:
    identity='fixture-journal'
    contract_version='tool-journal-v1'
    def __init__(self,phase=None):self.phase=phase;self.saved=None
    def claim(self,namespace,key,signature):
        if self.phase=='claim':raise RuntimeError('PRIVATE-JOURNAL')
        if self.phase=='uncertain':return ToolClaim('uncertain',signature)
        if self.saved is not None:return ToolClaim('replay',signature,outcome=deepcopy(self.saved))
        return ToolClaim('new',signature,'fenced-token')
    def commit(self,namespace,key,token,outcome):
        self.saved=deepcopy(outcome)
        if self.phase=='commit':raise RuntimeError('PRIVATE-JOURNAL')

def journaled(journal,handler=lambda args,ctx:ToolFailure('invalid_arguments')):
    events=[]
    tool=CustomTool('lookup','Lookup',SCHEMA,handler,'v1',with_context=True,operation_key=lambda a,c:'operation')
    result=execute_custom_tool(tool,'{"id":"ok"}',check_boundary=lambda:None,
        scope=InvocationScope('attempt',operation_namespace='tenant'),tool_journal=journal,on_outcome=events.append)
    return result,[o.to_dict() for o in events]

def test_journal_completion_and_replay_keep_original_envelope_and_classification():
    journal=Journal();calls=[]
    def handler(args,ctx):calls.append(1);return ToolFailure('invalid_arguments')
    first,events=journaled(journal,handler);second,replay=journaled(journal,handler)
    assert first==second==journal.saved==dict(status='error',error='invalid_arguments')
    assert calls==[1] and events[0]['code']==replay[0]['code']=='controlled_failure'
    assert events[0]['replayed'] is False and replay[0]['replayed'] is True
    assert replay[0]['phase']=='journal' and replay[0]['recovery']=='stop'

@pytest.mark.parametrize('phase,code',[('claim','tool_journal_failed'),('commit','tool_journal_failed'),('uncertain','tool_effect_uncertain')])
def test_journal_uncertainty_never_claims_a_safe_retry(phase,code):
    events=[];journal=Journal(phase)
    tool=CustomTool('lookup','Lookup',SCHEMA,lambda a,c:{},'v1',with_context=True,operation_key=lambda a,c:'operation')
    with pytest.raises(ToolEffectFailure):execute_custom_tool(tool,'{"id":"ok"}',check_boundary=lambda:None,
        scope=InvocationScope('attempt',operation_namespace='tenant'),tool_journal=journal,on_outcome=events.append)
    assert events[-1].to_dict()['code']==code and events[-1].to_dict()['recovery']=='reconcile'

def test_policy_flag_is_an_explicit_boolean():
    from prosaic_runtime import RunPolicy
    assert RunPolicy().bounded_tool_recovery is False
    for bad in [None,1,'bounded']:
        with pytest.raises(ValueError):RunPolicy(bounded_tool_recovery=bad)

@pytest.mark.parametrize('kind,code',[('ok','ok'),('controlled','controlled_failure'),('raise','handler_error')])
def test_structured_transport_reports_owned_terminal_outcomes(kind,code):
    import time
    from prosaic_runtime.structured_tools import StructuredToolLoop,ToolRequest,StructuredToolError
    events=[]
    def handler(args):
        if kind=='raise':raise RuntimeError('PRIVATE-STRUCTURED')
        return ToolFailure('invalid_arguments') if kind=='controlled' else {}
    tool=CustomTool('lookup','Lookup',SCHEMA,handler,'v1')
    loop=StructuredToolLoop({'lookup':tool},arguments={},deadline=time.monotonic()+10,
        allowed_tools={'lookup'},observer=events.append)
    if kind=='ok':loop.dispatch(ToolRequest('lookup',{'id':'ok'}))
    else:
        with pytest.raises(StructuredToolError):loop.dispatch(ToolRequest('lookup',{'id':'ok'}))
    completed=[e for e in events if e['event']=='tool_completed']
    assert len(completed)==1 and completed[0]['tool_outcome']['code']==code
    assert 'PRIVATE-STRUCTURED' not in str(events)
