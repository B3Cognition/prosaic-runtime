"""Actual public API tool batches overlap without changing wire correlation."""
from dataclasses import replace
import json
from threading import Barrier,get_ident
import pytest
import prosaic_runtime as api
from support.conversation_server import ConversationFixture
from test_conversation_tool_batches import queue_json,queue_stream,wire_call
from test_tool_evidence import bindings

SCHEMA={'type':'object','properties':{'i':{'type':'integer'}},'required':['i'],'additionalProperties':False}


def configure(fx,handler,**kw):
    fx.tools['lookup']=api.CustomTool('lookup','Read',SCHEMA,handler,'1',with_context=True,parallel_read=True,**kw)
    return replace(fx.policy,parallel_tools=api.ParallelToolPolicyV1(max_concurrency=2))


def result_blocks(fx):
    messages=fx.requests[-1]['messages']
    if fx.provider=='anthropic':
        groups=[m['content'] for m in messages if m['role']=='user' and type(m['content']) is list and any(b['type']=='tool_result' for b in m['content'])]
        assert len(groups)==1 and all(b['type']=='tool_result' for b in groups[0]);return groups[0]
    return [m for m in messages if m['role']=='tool']


@pytest.mark.parametrize('provider',['openai-compatible','anthropic'])
@pytest.mark.parametrize('streaming',[False,True])
def test_actual_parallel_batch_preserves_correlated_result_order(provider,streaming):
    with ConversationFixture(provider) as fx:
        gate=Barrier(2);workers=[];owner=get_ident();events=[]
        def read(a,c):workers.append(get_ident());gate.wait(timeout=1);return a
        policy=configure(fx,read)
        (queue_stream if streaming else queue_json)(fx,[wire_call('a',arguments='{"i":0}'),wire_call('b',arguments='{"i":1}')])
        (fx.queue_stream_text if streaming else fx.queue_text)('done')
        result=fx.runtime.run(fx.agent,policy=policy,tool_contract_bindings=bindings(),on_event=lambda e:events.append((get_ident(),e)))
        assert result.exit_code==0 and len(set(workers))==2 and owner not in workers
        assert [json.loads(b['content'])['result']['i'] for b in result_blocks(fx)]==[0,1]
        assert all(t==owner for t,e in events)
        assert result.metadata['parallel_tools_v1']['batches'][0]['calls'][0]['state']=='completed'
        assert result.metadata['invocation_budgets_v1']['tool_calls']==2


@pytest.mark.parametrize('provider',['openai-compatible','anthropic'])
def test_parallel_batch_cap_rejects_before_authorization_and_start(provider):
    with ConversationFixture(provider) as fx:
        seen=[];auth=[];events=[];policy=configure(fx,lambda a,c:seen.append(1),authorize=lambda a,c:auth.append(1) or True)
        policy=replace(policy,max_tool_calls=1)
        queue_json(fx,[wire_call('a',arguments='{"i":0}'),wire_call('b',arguments='{"i":1}')]);fx.queue_text('unused')
        result=fx.runtime.run(fx.agent,policy=policy,tool_contract_bindings=bindings(),on_event=events.append)
        assert result.exit_code!=0 and not seen and not auth and len(fx.requests)==1
        assert not any(e['event']=='tool_started' for e in events)


@pytest.mark.parametrize('provider',['openai-compatible','anthropic'])
def test_invalid_later_call_never_dispatches_prefix_in_parallel_run(provider):
    with ConversationFixture(provider) as fx:
        seen=[];policy=configure(fx,lambda a,c:seen.append(1))
        queue_json(fx,[wire_call('a',arguments='{"i":0}'),wire_call('b',name='forged',arguments='{}')]);fx.queue_text('unused')
        result=fx.runtime.run(fx.agent,policy=policy,tool_contract_bindings=bindings())
        assert result.exit_code!=0 and not seen and len(fx.requests)==1


def test_parallel_policy_without_explicit_contract_binding_is_rejected_before_provider():
    with ConversationFixture('openai-compatible') as fx:
        policy=configure(fx,lambda a,c:a);fx.queue_text('unused')
        with pytest.raises(ValueError,match='parallel tools require explicit'):
            fx.runtime.run(fx.agent,policy=policy)
        assert not fx.requests


def test_parallel_handler_cannot_reenter_public_runtime_execution():
    with ConversationFixture('openai-compatible') as fx:
        children=[]
        child=api.CustomTool('child','Read',{'type':'object','additionalProperties':False},lambda a:children.append(1),'1')
        runtime=api.ProsaicRuntime(fx.runtime.config,custom_tools={'child':child})
        def read(a,c):return runtime.run_tool('child',{},policy=api.RunPolicy(allowed_tools=frozenset({'child'})))
        policy=configure(fx,read);queue_json(fx,[wire_call('a',arguments='{"i":0}')]);fx.queue_text('unused')
        result=fx.runtime.run(fx.agent,policy=policy,tool_contract_bindings=bindings())
        assert result.exit_code!=0 and children==[] and len(fx.requests)==1


def test_unmarked_barrier_unknown_result_stops_later_parallel_window():
    with ConversationFixture('openai-compatible') as fx:
        seen=[];policy=configure(fx,lambda a,c:seen.append(a['i']) or a)
        def broken(a,c):seen.append('barrier');raise RuntimeError('private')
        fx.tools['barrier']=api.CustomTool('barrier','Read',SCHEMA,broken,'1',with_context=True)
        policy=replace(policy,allowed_tools=frozenset({'lookup','barrier'}))
        agent=fx.agent
        queue_json(fx,[wire_call('a',name='barrier',arguments='{"i":0}'),wire_call('b',arguments='{"i":1}')]);fx.queue_text('unused')
        result=fx.runtime.run(agent,policy=policy,tool_contract_bindings=bindings())
        assert result.exit_code!=0 and seen==['barrier'] and len(fx.requests)==1


@pytest.mark.parametrize('provider',['openai-compatible','anthropic'])
@pytest.mark.parametrize('concurrency',[1,2])
def test_bounded_recovery_stops_after_controlled_failure_and_drains_window(provider,concurrency):
    with ConversationFixture(provider) as fx:
        seen=[];gate=Barrier(2)
        def read(a,c):
            seen.append(a['i'])
            if concurrency==2:gate.wait(timeout=2)
            return api.ToolFailure('not_found') if a['i']==0 else a
        policy=configure(fx,read)
        policy=replace(policy,bounded_tool_recovery=True,parallel_tools=api.ParallelToolPolicyV1(max_concurrency=concurrency))
        n=2 if concurrency==1 else 3
        queue_json(fx,[wire_call(str(i),arguments=json.dumps({'i':i})) for i in range(n)]);fx.queue_text('unused')
        turn=fx.runtime.turn(fx.agent,message='Review',policy=policy,tool_contract_bindings=bindings())
        assert turn.result.exit_code!=0 and sorted(seen)==list(range(concurrency)) and len(fx.requests)==1
        calls=turn.evidence.to_dict()['parallelToolsV1']['schedule']['batches'][0]['calls']
        assert calls[0]['state']=='completed' and calls[0]['outcomeCode']=='controlled_failure'
        assert calls[concurrency]['state']=='notStarted'
        assert len(turn.evidence.to_dict()['toolOutcomes'])==concurrency


@pytest.mark.parametrize('provider',['openai-compatible','anthropic'])
def test_unmarked_serial_barrier_observes_owner_cancellation(provider):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    with ConversationFixture(provider) as fx:
        entered=Event();release=Event();cancel=Event();seen=[]
        def read(a,c):entered.set();assert release.wait(3);seen.append(c.cancelled());return a
        policy=configure(fx,read)
        original=fx.tools['lookup'];fx.tools['lookup']=api.CustomTool(original.name,original.description,original.parameters,read,'1',with_context=True)
        queue_json(fx,[wire_call('a',arguments='{"i":0}')]);fx.queue_text('unused')
        with ThreadPoolExecutor(1) as pool:
            future=pool.submit(fx.runtime.run,fx.agent,policy=policy,tool_contract_bindings=bindings(),cancelled=cancel.is_set)
            try:assert entered.wait(2);cancel.set()
            finally:release.set()
            result=future.result(timeout=4)
        assert seen==[True] and result.exit_code!=0 and len(fx.requests)==1
