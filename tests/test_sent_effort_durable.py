"""Published Harness retains additive result evidence without changed receipts."""
from copy import deepcopy
from uuid import uuid4
import pytest
from prosaic_runtime import ExecutionContext, ProsaicArtifact
from prosaic_runtime.accounting import MemoryRecorder
from prosaic_harness import Harness, FileRunStore, WorkflowFactory, WorkflowCatalog, WorkflowBindings, WorkflowPolicy
from test_sent_effort import wire,runner,KEY


def workflow(rt):
    a=ProsaicArtifact('approved','subagent',{'name':'approved','description':'Synthetic result evidence','model_tier':'fast','tools':[]},'Return a JSON object.')
    return WorkflowFactory(catalog=WorkflowCatalog(agents={'author':a},schemas={'result':{'type':'object','additionalProperties':True}}),
        bindings=WorkflowBindings(config=rt.config),
        policy=WorkflowPolicy(allowed_agents={'author'},allowed_schemas={'result'},allowed_model_tiers={'fast'},allow_agent_composition=True,
            max_calls=2,max_attempts=1,max_provider_requests_per_invocation=1,max_tool_calls_per_invocation=0)).build({
        'version':1,'name':'sent-effort','start':'first','steps':{
        'first':{'kind':'agent','agent':'author','schema':'result','next':'approval'},
        'approval':{'kind':'pause','question':'Continue?','choices':{'approve':'second'}},
        'second':{'kind':'agent','agent':'author','schema':'result','next':'done'},'done':{'kind':'finish'}}})


def test_file_receipts_keep_trace_after_pause_reopen_and_lost_ack(tmp_path,wire):
    fx,_=wire;rec=MemoryRecorder(provider_id=fx.provider);rt=runner(fx,default='low' if fx.provider=='openai-compatible' else None,accounting=rec)
    from dataclasses import replace
    rt.config=replace(rt.config,routes={'fast':'local'})
    flow=workflow(rt);run_id=uuid4().hex;saved=[]
    class LostAck(BaseException):pass
    class FaultStore(FileRunStore):
        def save_receipt(self,run_id,invocation_id,receipt,lease):
            super().save_receipt(run_id,invocation_id,receipt,lease);saved.append(deepcopy(receipt))
            if receipt['step']=='second':raise LostAck()
    store=FaultStore(tmp_path/'store',namespace='effort');store.initialize()
    try:
        fx.queue_text('{}');fx.queue_text('{}')
        h=Harness(flow,store=store,run_id=run_id,runtime=rt,context=ExecutionContext(tenant_id='tenant',request_metadata={'variant':'original'}))
        assert h.run({})['status']=='waiting'
        store.close();store=FaultStore(tmp_path/'store',namespace='effort');store.initialize()
        h=Harness(flow,store=store,run_id=run_id,runtime=rt)
        with pytest.raises(LostAck):h.resume(choice='approve',expected_revision=h.status().revision)
        assert len(saved)==len(fx.requests)==2
        documents=[r['result']['metadata'][KEY] for r in saved]
        assert all(d['attempt_count']==1 and d['complete'] is True for d in documents)
        assert {d['attempts'][0]['provider_call_id'] for d in documents}==set(rec.intents)
        store.close();store=FileRunStore(tmp_path/'store',namespace='effort');store.initialize()
        fresh=Harness(flow,store=store,run_id=run_id,runtime=rt)
        assert fresh.recovery_evidence()['recoveryStatus']=='receiptAvailable'
        assert fresh.resume()['status']=='completed' and len(fx.requests)==2
        for receipt in saved:
            assert store.load_receipt(run_id,receipt['id'])['result']['metadata'][KEY]==receipt['result']['metadata'][KEY]
    finally:store.close()


def test_public_0917_workflow_fingerprint_is_unchanged():
    from prosaic_runtime import EndpointConfig,RuntimeConfig,ProsaicRuntime
    rt=ProsaicRuntime(RuntimeConfig({'local':EndpointConfig('http://127.0.0.1:9/v1','example',features={'streaming':False,'reasoning_effort':'low'})},{'fast':'local'},'local'),source='/private/tmp/sent-effort-golden-project/.prosaic')
    flow=workflow(rt)
    assert flow.fingerprint==flow.current_fingerprint()=='7450ebf3b7bcbe82db48544dc27d1b5c2b245aceaa6a41d9651c0f2f3cf69557'
