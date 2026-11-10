from dataclasses import replace
import json,re
from threading import Barrier,Event
import pytest
import prosaic_runtime as api
from support.conversation_server import ConversationFixture
from test_conversation_tool_batches import queue_json,wire_call
from test_parallel_tool_wires import configure
from test_tool_evidence import bindings
from support.rules_support import runtime_for,tagged


@pytest.mark.parametrize('denied',[None,'cite','modelUse'])
def test_parallel_typed_results_become_citable_only_after_actual_delivery(denied):
    with ConversationFixture('openai-compatible') as fx:
        gate=Barrier(2)
        def read(a,c):
            gate.wait(timeout=2)
            return api.ToolResultV1(data={'i':a['i']},evidence=(api.EvidenceDraftV1('source'+str(a['i']),'r1','private:source','/i','Result'),))
        policy=configure(fx,read,result_contract=api.ToolResultContractV1('1',{'type':'object'}))
        queue_json(fx,[wire_call('a',arguments='{"i":0}'),wire_call('b',arguments='{"i":1}')]);fx.queue_text('placeholder')
        class Replies(list):
            def pop(self,index=0):
                value=super().pop(index);return value() if callable(value) else value
        fx.responses=Replies(fx.responses)
        def answer():
            ids=re.findall(r'ev_[0-9a-f]{64}',fx.raw_requests[-1].decode());assert len(set(ids))==2
            fx.queue_text('Results '+','.join('[cite:'+i+']' for i in dict.fromkeys(ids)))
            return list.pop(fx.responses)
        fx.responses[1]=answer
        binding=replace(bindings(lambda use:use.purpose!=denied),citations=api.CitationPolicyV1('required'))
        result=fx.runtime.run(fx.agent,policy=policy,tool_contract_bindings=binding)
        if denied is None:
            assert result.exit_code==0 and api.CitationsV1.from_dict(result.metadata['citations_v1']).verify_answer(result.stdout)
            assert [r['callIndex'] for r in result.metadata['tool_contract_v1']['records']]==[0,1]
        else:
            assert result.exit_code!=0 and result.stdout=='' and result.metadata['failure_reason']=='evidenceUseDenied'
            assert len(fx.requests)==(1 if denied=='modelUse' else 2)
        assert len(result.metadata['tool_contract_v1']['calls'])==2


def test_parallel_after_tool_guards_observe_wire_order():
    with ConversationFixture('openai-compatible') as fx:
        gate=Barrier(2);seen=[]
        def read(a,c):gate.wait(timeout=2);return a
        policy=configure(fx,read)
        guard=api.Guard('ordered','1',('after_tool',),lambda stage,ctx:seen.append(ctx.tool_result['result']['i']) or api.Verdict(True))
        agent=tagged('- ALWAYS preserve order. <!-- enforce: ordered(stage=after_tool) -->',tools=['lookup'])
        queue_json(fx,[wire_call('a',arguments='{"i":0}'),wire_call('b',arguments='{"i":1}')]);fx.queue_text('done')
        turn=runtime_for(fx,guards=[guard]).turn(agent,message='Review',policy=policy,tool_contract_bindings=bindings())
        assert turn.result.exit_code==0 and seen==[0,1]


@pytest.mark.parametrize('retry',[False,True])
def test_after_tool_stop_and_repair_feedback_keep_existing_allowances(retry):
    with ConversationFixture('openai-compatible') as fx:
        seen=[];gate=Barrier(2)
        def read(a,c):gate.wait(timeout=2);seen.append(a['i']);return a
        policy=configure(fx,read)
        guard=api.Guard('reject','1',('after_tool',),lambda stage,ctx:api.Verdict(False))
        suffix=', on_violation=retry' if retry else ', on_violation=fail'
        agent=tagged('- ALWAYS reject. <!-- enforce: reject(stage=after_tool'+suffix+') -->',tools=['lookup'])
        queue_json(fx,[wire_call('a',arguments='{"i":0}'),wire_call('b',arguments='{"i":1}')]);fx.queue_text('done')
        turn=runtime_for(fx,guards=[guard]).turn(agent,message='Review',policy=policy,tool_contract_bindings=bindings())
        assert sorted(seen)==[0,1] and turn.evidence.to_dict()['toolAttempts']==2
        if retry:
            assert turn.result.exit_code==0 and len(fx.requests)==2
            results=[json.loads(m['content']) for m in fx.requests[1]['messages'] if m['role']=='tool']
            assert all(m['error']=='rule_refused' and 'ruleFeedback' in m for m in results)
        else:assert turn.result.exit_code!=0 and len(fx.requests)==1
