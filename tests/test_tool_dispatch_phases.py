"""Only the handler phase crosses a worker boundary; settlement keeps authority."""
from concurrent.futures import ThreadPoolExecutor
import json
from threading import get_ident
import time
import pytest
from prosaic_runtime import CustomTool, InvocationScope, ToolClaim, ToolFailure, ToolResultV1, ToolResultContractV1
from prosaic_runtime.tool_dispatch import (prepare_custom_tool_call, invoke_prepared_tool,
    settle_custom_tool_call, start_prepared_tool, PreparedToolCallV1)
from prosaic_runtime.tool_effects import ToolEffectFailure

SCHEMA = {'type': 'object', 'additionalProperties': False}


class Journal:
    def __init__(self): self.claimed = self.saved = None
    def claim(self, namespace, key, signature):
        self.claimed = (namespace, key, signature, get_ident())
        return ToolClaim('new', signature, 'token')
    def commit(self, namespace, key, token, outcome): self.saved = (namespace, key, token, outcome, get_ident())


def prepare(tool, **kw):
    options = dict(check_boundary=lambda: None, scope=InvocationScope('invocation', operation_namespace='tenant'),
        deadline=time.monotonic()+10, cancelled=lambda: False, **kw)
    return prepare_custom_tool_call(tool, '{}', call_id='call', call_index=0, batch_index=0, options=options)


def test_phases_keep_all_nonhandler_callbacks_on_owner():
    owner = get_ident(); observed = []; journal = Journal()
    def handler(args, ctx): observed.append(('handler', get_ident())); return {'count': 14}
    tool = CustomTool('lookup', 'Lookup', SCHEMA, handler, '1', with_context=True,
        authorize=lambda a,c: observed.append(('authorize',get_ident())) or True,
        operation_key=lambda a,c: observed.append(('key',get_ident())) or 'key')
    call = prepare(tool, tool_journal=journal, on_completion=lambda *a,**k: observed.append(('completion',get_ident())))
    assert isinstance(call, PreparedToolCallV1) and not journal.saved
    start_prepared_tool(call)
    with ThreadPoolExecutor(1) as pool: result = pool.submit(invoke_prepared_tool,call).result()
    assert journal.saved is None
    settled = settle_custom_tool_call(call,result)
    assert settled.outcome == {'status':'ok','result':{'count':14}}
    assert journal.saved[-1] == journal.claimed[-1] == owner
    assert all(ident==owner for kind,ident in observed if kind!='handler')
    assert next(i for k,i in observed if k=='handler') != owner


@pytest.mark.parametrize('typed', [False, True])
def test_owned_bounded_result_survives_late_mutation(typed):
    original = {'count':14}
    tool = CustomTool('lookup','Lookup',SCHEMA,lambda a: ToolResultV1(data=original) if typed else original,'1',
        result_contract=ToolResultContractV1('1',{'type':'object'}) if typed else None)
    call=prepare(tool); start_prepared_tool(call); result=invoke_prepared_tool(call); original['count']=99
    saved=settle_custom_tool_call(call,result); first=saved.outcome
    assert (first['result']['data'] if typed else first['result'])['count']==14
    first.clear(); assert saved.outcome['status']=='ok'


def test_denied_authorization_never_claims_or_invokes():
    journal=Journal(); seen=[]
    tool=CustomTool('lookup','Lookup',SCHEMA,lambda a,c: seen.append(1),'1',with_context=True,
        authorize=lambda a,c: False,operation_key=lambda a,c:'key')
    result=prepare(tool,tool_journal=journal)
    assert result.code=='authorization_denied' and not seen and journal.claimed is None


def test_completed_journal_survives_failed_completion_callback():
    journal=Journal()
    tool=CustomTool('lookup','Lookup',SCHEMA,lambda a,c: {'ok':True},'1',with_context=True,operation_key=lambda a,c:'key')
    def fail(*a,**kw): raise RuntimeError('sink failed')
    call=prepare(tool,tool_journal=journal,on_completion=fail); start_prepared_tool(call)
    result=invoke_prepared_tool(call)
    with pytest.raises(RuntimeError,match='sink failed'): settle_custom_tool_call(call,result)
    assert journal.saved[3]=={'status':'ok','result':{'ok':True}}


def test_unexpected_journaled_exception_is_uncertain():
    def fail(a,c): raise ValueError('PRIVATE')
    tool=CustomTool('lookup','Lookup',SCHEMA,fail,'1',with_context=True,operation_key=lambda a,c:'key')
    seen=[]; journal=Journal(); call=prepare(tool,tool_journal=journal,on_completion=lambda *a,**kw:seen.append((a,kw)))
    start_prepared_tool(call); result=invoke_prepared_tool(call)
    with pytest.raises(ToolEffectFailure): settle_custom_tool_call(call,result)
    assert journal.saved is None and seen[0][0]==(None,'uncertain')


def test_controlled_failure_remains_known_completion():
    tool=CustomTool('lookup','Lookup',SCHEMA,lambda a:ToolFailure('missing'),'1')
    call=prepare(tool); start_prepared_tool(call)
    result=settle_custom_tool_call(call,invoke_prepared_tool(call))
    assert result.code=='controlled_failure' and result.outcome['status']=='error'
