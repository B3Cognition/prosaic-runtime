"""Nested real Runtime calls share dispatch/deadline limits and trusted lineage."""
import time
from dataclasses import replace
import pytest
from prosaic_runtime import CustomTool, EndpointConfig, ProsaicRuntime, RunPolicy, RuntimeConfig, ExecutionContext
from prosaic_runtime.accounting import MemoryRecorder
from test_runtime import server, artifact, completion, runtime
from test_custom_tool_transport import call


def test_sequential_invocations_cannot_reset_shared_provider_allowance(server):
    from prosaic_runtime import ExecutionBudget
    url,requests,responses=server
    responses.extend([completion('one'),completion('two')])
    runner=runtime(EndpointConfig(url,'synthetic',features={'streaming':False}))
    budget=ExecutionBudget(timeout_s=10,max_provider_requests=1,max_tool_calls=0)
    with budget.activate():
        assert runner.run(artifact()).exit_code==0
        stopped=runner.run(artifact())
    assert stopped.metadata['failure_reason']=='provider_request_limit' and len(requests)==1
    assert budget.provider_requests==1
    # Leaving a scope restores normal execution without replenishing the old budget.
    assert runner.run(artifact()).exit_code==0 and len(requests)==2
    with budget.activate(): assert runner.run(artifact()).exit_code!=0
    assert len(requests)==2


def test_nested_turn_planner_and_retry_share_budget_and_accounting(server):
    from prosaic_runtime import ExecutionBudget
    url,requests,responses=server
    recorder=MemoryRecorder()
    seen=[]
    cfg=RuntimeConfig({'local':EndpointConfig(url,'synthetic',features={'streaming':False})},
        {'fast':'local'},'local',frozenset({'lookup_catalog'}))
    def planner(args,ctx):
        seen.append(ctx)
        child=ctx.child_context(invocation_id='planner-call',run_id='planner-run',step_id='compose')
        result=ProsaicRuntime(cfg).run(artifact(),context=child,accounting=recorder,
            policy=RunPolicy(timeout_s=ctx.remaining_timeout(10)))
        assert result.exit_code==0
        denied=ProsaicRuntime(cfg).run(artifact(),context=child,accounting=recorder)
        assert denied.metadata['failure_reason']=='provider_request_limit'
        return {'proposal':'preview'}
    tool=CustomTool('lookup_catalog','Bounded planner',{'type':'object','properties':{'sku':{'type':'string'}},
        'required':['sku'],'additionalProperties':False},planner,'v1',with_context=True)
    runner=ProsaicRuntime(cfg,custom_tools={'lookup_catalog':tool})
    responses.extend([completion('',[call()]),completion('preview'),completion('unused')])
    budget=ExecutionBudget(timeout_s=10,max_provider_requests=2,max_tool_calls=1)
    ctx=ExecutionContext(application_id='app',tenant_id='tenant',billing_account_id='payer',actor_id='actor')
    with budget.activate():
        turn=runner.turn(replace(artifact(['lookup_catalog']),body='ALWAYS use the approved planner. NEVER apply a preview.'),message='compose',context=ctx,accounting=recorder,
            policy=RunPolicy(allowed_tools=frozenset({'lookup_catalog'}),max_provider_requests=2,max_tool_calls=1))
    assert turn.result.exit_code!=0 and len(requests)==2 and budget.provider_requests==2
    assert len(seen)==1
    assert len(recorder.intents)==2
    child=next(i for i in recorder.intents.values() if i['context']['run_id']=='planner-run')
    assert child['context']['parent_invocation_id']==seen[0].accounting_context.invocation_id
    assert child['context']['tenant_id']=='tenant' and child['context']['billing_account_id']=='payer'


def test_expired_shared_deadline_stops_before_provider(server):
    from prosaic_runtime import ExecutionBudget
    url,requests,responses=server
    budget=ExecutionBudget(timeout_s=.001,max_provider_requests=3,max_tool_calls=0)
    time.sleep(.002)
    with budget.activate():
        result=runtime(EndpointConfig(url,'test')).run(artifact())
    assert result.timed_out and requests==[]


def test_inner_scope_cannot_replace_parent_budget(server):
    from prosaic_runtime import ExecutionBudget
    url,requests,responses=server
    outer=ExecutionBudget(timeout_s=10,max_provider_requests=0,max_tool_calls=0)
    inner=ExecutionBudget(timeout_s=10,max_provider_requests=20,max_tool_calls=20)
    with outer.activate(),inner.activate():
        result=runtime(EndpointConfig(url,'test')).run(artifact())
    assert result.exit_code!=0 and requests==[] and inner.provider_requests==0


def test_shared_allowance_is_atomic_across_explicit_worker_scopes(server):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from prosaic_runtime import ExecutionBudget
    url,requests,responses=server
    responses.append(completion('only once'))
    budget=ExecutionBudget(timeout_s=10,max_provider_requests=1,max_tool_calls=0)
    barrier=Barrier(2)
    def invoke():
        runner=runtime(EndpointConfig(url,'test',features={'streaming':False}))
        with budget.activate():
            barrier.wait(timeout=3)
            return runner.run(artifact())
    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes=list(workers.map(lambda _:invoke(),range(2)))
    assert sorted(r.exit_code for r in outcomes)==[0,1]
    assert len(requests)==1 and budget.provider_requests==1


def test_parent_cancellation_prevents_nested_inference(server):
    from prosaic_runtime import ExecutionBudget
    url,requests,responses=server
    budget=ExecutionBudget(timeout_s=10,max_provider_requests=2,max_tool_calls=0,cancelled=lambda:True)
    with budget.activate():
        result=runtime(EndpointConfig(url,'test')).run(artifact())
    assert result.exit_code!=0 and result.metadata['failure_reason']=='cancelled'
    assert requests==[]


@pytest.mark.parametrize('usage,cap,reason,known',[(7,7,'token_limit',7),(None,10,'usage_unknown',0),(7,6,'token_limit',7)])
def test_shared_reported_tokens_preserve_unknown_and_paid_partial_evidence(server,usage,cap,reason,known):
    import json
    from prosaic_runtime import ExecutionBudget
    url,requests,responses=server
    kind,body=completion('answer')
    item=json.loads(body)
    if usage is None: item.pop('usage')
    responses.append((kind,json.dumps(item).encode()))
    budget=ExecutionBudget(timeout_s=10,max_provider_requests=3,max_tool_calls=0,max_reported_tokens=cap)
    runner=runtime(EndpointConfig(url,'test',features={'streaming':False}))
    with budget.activate():
        first=runner.run(artifact())
        second=runner.run(artifact())
    assert len(requests)==1 and budget.reported_tokens==known
    assert budget.usage_complete==(usage is not None)
    assert second.metadata['failure_reason']==reason
    assert first.token_usage==usage
    assert first.exit_code==(0 if usage is not None and usage<=cap else 1)
