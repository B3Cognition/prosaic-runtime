from copy import deepcopy
from dataclasses import replace
from threading import Barrier
import pytest
import prosaic_runtime as api
from support.conversation_server import ConversationFixture
from test_tool_evidence import bindings
from test_parallel_tool_wires import configure
from test_conversation_tool_batches import queue_json,wire_call


def test_parallel_frozen_identity_roundtrip_and_drift():
    with ConversationFixture('openai-compatible') as fx:
        policy=configure(fx,lambda a,c:a);runtime=fx.runtime
        prepared=runtime.prepare_turn(fx.agent,message='Review',policy=policy,tool_contract_bindings=bindings())
        assert prepared.encoding_version=='conversation-turn-v5'
        frozen=prepared.export_frozen_input().to_dict()
        assert frozen['version']==3 and frozen['bindings']['parallelToolsV1']['policy']==policy.parallel_tools.to_dict()
        assert 'parallel_tool_calls_v1' in runtime.capabilities
        restored=runtime.restore_turn(fx.agent,frozen_input=frozen,policy=policy,limits=api.TurnLimits(),tool_contract_bindings=bindings())
        assert restored.context_identity==prepared.context_identity
        for other in (replace(policy,parallel_tools=None),replace(policy,parallel_tools=api.ParallelToolPolicyV1(max_concurrency=1))):
            with pytest.raises(api.TurnAdmissionError):runtime.restore_turn(fx.agent,frozen_input=frozen,policy=other,limits=api.TurnLimits(),tool_contract_bindings=bindings())
        for version,encoding in ((1,'conversation-turn-v3'),(2,'conversation-turn-v4')):
            forged=deepcopy(frozen);forged.update(version=version,encodingVersion=encoding)
            with pytest.raises(ValueError):api.FrozenTurnInput.from_dict(forged)
        old=runtime.prepare_turn(fx.agent,message='Review',policy=replace(policy,parallel_tools=None),tool_contract_bindings=bindings()).export_frozen_input().to_dict()
        assert old['version']==2 and 'parallelToolsV1' not in old['bindings']


@pytest.mark.parametrize('provider',['openai-compatible','anthropic'])
def test_parallel_evidence_roundtrips_with_closed_observation(provider):
    with ConversationFixture(provider) as fx:
        gate=Barrier(2)
        def read(a,c):gate.wait(timeout=2);return a
        policy=configure(fx,read)
        queue_json(fx,[wire_call('a',arguments='{"i":0}'),wire_call('b',arguments='{"i":1}')]);fx.queue_text('done')
        turn=fx.runtime.turn(fx.agent,message='Review',policy=policy,tool_contract_bindings=bindings())
        assert turn.result.exit_code==0
        doc=turn.evidence.to_dict();assert doc['version']==4
        assert doc['parallelToolsV1']['bindings']['policy']==policy.parallel_tools.to_dict()
        assert [c['callIndex'] for c in doc['parallelToolsV1']['schedule']['batches'][0]['calls']]==[0,1]
        assert api.TurnEvidence.from_dict(doc).to_dict()==doc
        for mutate in (lambda d:d.pop('parallelToolsV1'),lambda d:d['parallelToolsV1']['schedule']['batches'][0]['calls'][0].update(state=[]),lambda d:d['parallelToolsV1']['bindings']['policy'].update(maxConcurrency=1),lambda d:d['parallelToolsV1'].update(authorize=True)):
            bad=deepcopy(doc);mutate(bad)
            with pytest.raises(ValueError):api.TurnEvidence.from_dict(bad)


@pytest.mark.parametrize('field',['state','journalState','outcomeCode'])
def test_observation_codec_rejects_nonscalar_status(field):
    from prosaic_runtime.tool_parallel_contracts import validate_parallel_event
    d=dict(version=1,batchId='a'*64,callId='b'*64,callIndex=0,batchIndex=0,state='completed',handlerEntered=True,journalState='notUsed',outcomeCode='ok',durationMs=0)
    d[field]=[]
    with pytest.raises(ValueError):validate_parallel_event(d)


def test_failed_turn_retains_known_sibling_and_all_schedule_slots():
    from threading import Event
    with ConversationFixture('openai-compatible') as fx:
        done=Event()
        def read(a,c):
            if a['i']==0:done.set();return {'saved':14}
            assert done.wait(2);raise RuntimeError('PRIVATE')
        policy=configure(fx,read)
        queue_json(fx,[wire_call('a',arguments='{"i":0}'),wire_call('b',arguments='{"i":1}'),wire_call('c',arguments='{"i":2}')]);fx.queue_text('unused')
        turn=fx.runtime.turn(fx.agent,message='Review',policy=policy,tool_contract_bindings=bindings())
        doc=turn.evidence.to_dict();assert turn.result.exit_code!=0 and turn.completed_turn is None
        calls=doc['parallelToolsV1']['schedule']['batches'][0]['calls']
        assert [c['state'] for c in calls]==['completed','uncertain','notStarted']
        assert doc['toolAttempts']==2 and any(o.get('outcome',{}).get('result')=={'saved':14} for o in doc['toolOutcomes'])
        assert len(fx.requests)==1


def test_unstarted_cursor_still_pins_admitted_call_order():
    from prosaic_runtime.tool_parallel import ParallelToolCoordinator
    from parallel_tool_helpers import coordinator,call,tool
    calls=(call(0),call(1));coord,_=coordinator({'lookup':tool('lookup',lambda a,c:a)},calls)
    coord.plan(calls)
    assert [c['state'] for c in coord.evidence['calls']]==['notStarted','notStarted']
    coord.close()
