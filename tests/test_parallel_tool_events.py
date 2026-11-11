from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from threading import Barrier,Event,get_ident
import pytest
import prosaic_runtime as api
from support.conversation_server import ConversationFixture
from test_conversation_tool_batches import queue_json,wire_call
from test_parallel_tool_wires import configure
from test_tool_evidence import bindings


@pytest.mark.parametrize('provider',['openai-compatible','anthropic'])
def test_fast_settlement_is_live_without_payload_and_sink_is_serial(provider):
    with ConversationFixture(provider) as fx:
        fast=Event();release=Event();gate=Barrier(2);events=[];owners=[]
        def read(a,c):
            gate.wait(timeout=2)
            if a['i']==0:assert release.wait(3)
            return {'count':a['i'],'private':'PRIVATE-PAYLOAD'}
        policy=configure(fx,read);queue_json(fx,[wire_call('slow',arguments='{"i":0}'),wire_call('fast',arguments='{"i":1}')]);fx.queue_text('done')
        def sink(event):
            events.append(event);owners.append(get_ident())
            if event['event']=='tool_execution_settled' and event['parallelToolV1']['batchIndex']==1:fast.set()
        with ThreadPoolExecutor(1) as pool:
            future=pool.submit(fx.runtime.run,fx.agent,policy=policy,tool_contract_bindings=bindings(),on_event=sink)
            try:assert fast.wait(2) and not future.done()
            finally:release.set()
            result=future.result(timeout=3)
        assert result.exit_code==0 and len(set(owners))==1
        settlement=[e for e in events if e['event']=='tool_execution_settled']
        assert [e['parallelToolV1']['batchIndex'] for e in settlement]==[1,0]
        assert 'PRIVATE-PAYLOAD' not in json.dumps(settlement) and all(len(json.dumps(e).encode())<=1024 for e in settlement)


def test_new_worker_observer_callbacks_run_only_on_invocation_owner():
    with ConversationFixture('openai-compatible') as fx:
        owner=get_ident();seen=[];gate=Barrier(2)
        policy=configure(fx,lambda a,c:gate.wait(timeout=2) or a)
        queue_json(fx,[wire_call('a',arguments='{"i":0}'),wire_call('b',arguments='{"i":1}')]);fx.queue_text('done')
        result=fx.runtime.run(fx.agent,policy=policy,tool_contract_bindings=bindings(),observer=lambda e:seen.append((get_ident(),e)))
        assert result.exit_code==0 and seen and all(t==owner for t,e in seen)


def test_public_turn_forwards_bounded_parallel_settlement_events():
    with ConversationFixture('openai-compatible') as fx:
        events=[];gate=Barrier(2)
        def read(a,c):gate.wait(timeout=2);return a
        policy=configure(fx,read)
        queue_json(fx,[wire_call('a',arguments='{"i":0}'),wire_call('b',arguments='{"i":1}')]);fx.queue_text('done')
        turn=fx.runtime.turn(fx.agent,message='Review',policy=policy,tool_contract_bindings=bindings(),on_event=events.append)
        assert turn.result.exit_code==0
        settled=[e for e in events if e['event']=='tool_execution_settled']
        assert len(settled)==2 and {e['parallelToolV1']['callIndex'] for e in settled}=={0,1}


def test_parallel_journal_replay_emits_no_second_start(tmp_path):
    from support.tool_journal import SqliteJournal
    with ConversationFixture('openai-compatible') as fx:
        events=[];seen=[]
        policy=configure(fx,lambda a,c:seen.append(1) or a,operation_key=lambda a,c:'key',
            event_policy=api.ToolEventPolicyV1('1',{}))
        queue_json(fx,[wire_call('a',arguments='{"i":0}'),wire_call('b',arguments='{"i":0}')]);fx.queue_text('done')
        result=fx.runtime.run(fx.agent,policy=policy,tool_contract_bindings=bindings(),on_event=events.append,
            tool_journal=SqliteJournal(tmp_path/'journal.sqlite'))
        assert result.exit_code==0 and seen==[1]
        assert len([e for e in events if e['event']=='tool_started'])==1
        assert [e['toolEventV1']['phase'] for e in events if e['event']=='tool_completed']==['completed','replayed']


@pytest.mark.parametrize('denied',[False,True])
def test_serial_barrier_start_event_requires_handler_entry(tmp_path,denied):
    from support.tool_journal import SqliteJournal
    with ConversationFixture('openai-compatible') as fx:
        seen=[];events=[]
        policy=configure(fx,lambda a,c:seen.append(1) or a,operation_key=lambda a,c:'key',authorize=lambda a,c:not denied)
        original=fx.tools['lookup']
        fx.tools['lookup']=api.CustomTool(original.name,original.description,original.parameters,original.handler,original.version,with_context=True,authorize=original.authorize,operation_key=original.operation_key)
        queue_json(fx,[wire_call('a',arguments='{"i":0}'),wire_call('b',arguments='{"i":0}')]);fx.queue_text('done')
        result=fx.runtime.run(fx.agent,policy=policy,tool_contract_bindings=bindings(),on_event=events.append,tool_journal=SqliteJournal(tmp_path/'journal.sqlite'))
        assert result.exit_code==0 and seen==([] if denied else [1])
        assert len([e for e in events if e['event']=='tool_started'])==(0 if denied else 1)
