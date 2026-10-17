"""Synthetic counters exercise admission, not production tokenizer accuracy."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from prosaic_runtime import ExecutionBudget, EndpointConfig, RunPolicy
from prosaic_runtime.budgets import InvocationBudgetExceeded
from prosaic_runtime.execution_budget import reserve_dispatch, record_shared_usage
from support.context_counter import fixture_context
from test_runtime import server, runtime, artifact, completion
from test_anthropic import native_server


def registration(count=3, *, model='test', provider='openai-compatible'):
    policy, registry = fixture_context(provider, model=model, count=lambda request: count)
    return registry.resolve(policy.counter_identity)


def budget(cap=10, **kw):
    return ExecutionBudget(timeout_s=10, max_provider_requests=5, max_tool_calls=2,
                           max_reported_tokens=cap, token_counter=registration(), **kw)


def test_rejects_request_before_http_and_does_not_debit(server):
    url, requests, responses = server
    b=budget(7)
    with b.activate():
        result=runtime(EndpointConfig(url,'test',max_tokens=5)).run(artifact())
    assert result.metadata['failure_reason']=='token_limit'
    assert requests==[] and b.provider_requests==0 and b.reserved_tokens==0


def test_exact_fit_settles_actual_and_releases_only_unused_tokens(server):
    url, requests, responses = server
    responses.append(completion('yes'))  # 7 reported units
    b=budget(8)
    with b.activate():
        result=runtime(EndpointConfig(url,'test',max_tokens=5,features={'streaming':False})).run(artifact())
    assert result.exit_code==0 and b.reported_tokens==7 and b.reserved_tokens==0
    assert b.usage_complete and len(requests)==1


@pytest.mark.parametrize('endpoint', [dict(model='other',max_tokens=5),dict(model='test',max_tokens=None)])
def test_missing_bound_or_wrong_counter_binding_is_closed(server,endpoint):
    url, requests, responses = server
    b=budget()
    with b.activate():
        result=runtime(EndpointConfig(url,**endpoint)).run(artifact())
    assert result.metadata['failure_reason']=='token_admission'
    assert requests==[] and b.provider_requests==0


def test_invocation_remainder_cannot_be_broadened_by_outer_scope(server):
    url, requests, responses = server
    b=budget(100)
    with b.activate():
        result=runtime(EndpointConfig(url,'test',max_tokens=5)).run(artifact(),policy=RunPolicy(max_reported_tokens=7))
    assert result.metadata['failure_reason']=='token_limit'
    assert requests==[] and b.provider_requests==0


def test_provider_bound_breach_retains_truth_and_stops_even_with_headroom(server):
    url, requests, responses = server
    responses.append(completion('yes'))
    b=ExecutionBudget(timeout_s=10,max_provider_requests=5,max_tool_calls=0,
        max_reported_tokens=100,token_counter=registration(1))
    with b.activate():
        runner=runtime(EndpointConfig(url,'test',max_tokens=5,features={'streaming':False}))
        first=runner.run(artifact()); second=runner.run(artifact())
    assert first.token_usage==7 and first.metadata['failure_reason']=='token_reservation_exceeded'
    assert second.metadata['failure_reason']=='token_reservation_exceeded'
    assert b.reported_tokens==7 and b.reserved_tokens==0 and len(requests)==1


def test_unknown_usage_retains_reservation_and_blocks_dispatch(server):
    url, requests, responses = server
    kind, raw=completion('yes'); parsed=json.loads(raw); parsed.pop('usage')
    responses.append((kind,json.dumps(parsed).encode()))
    b=budget()
    with b.activate():
        runner=runtime(EndpointConfig(url,'test',max_tokens=5,features={'streaming':False}))
        first=runner.run(artifact()); second=runner.run(artifact())
    assert first.token_usage is None and not b.usage_complete and b.reserved_tokens==8
    assert second.metadata['failure_reason']=='usage_unknown' and len(requests)==1


def test_atomic_inflight_tokens_across_worker_scopes():
    b=budget(10); barrier=Barrier(2)
    def dispatch():
        barrier.wait(timeout=3)
        try:return reserve_dispatch((b,),token_reservations={b:8})
        except InvocationBudgetExceeded:return None
    with ThreadPoolExecutor(max_workers=2) as pool: tickets=list(pool.map(lambda _:dispatch(),range(2)))
    assert sum(t is not None for t in tickets)==1
    assert b.reserved_tokens==8 and b.provider_requests==1
    record_shared_usage((b,),7,reservation=next(t for t in tickets if t is not None))
    assert b.reserved_tokens==0 and b.reported_tokens==7


def test_enclosing_legacy_budget_also_reserves_strict_child_tokens():
    outer=ExecutionBudget(timeout_s=10,max_provider_requests=5,max_tool_calls=0,max_reported_tokens=7)
    inner=budget(100)
    with pytest.raises(InvocationBudgetExceeded,match='token_limit'):
        reserve_dispatch((outer,inner),token_reservations={outer:8,inner:8})
    assert outer.provider_requests==inner.provider_requests==0


def test_strict_budget_requires_token_ceiling():
    with pytest.raises(ValueError):
        ExecutionBudget(timeout_s=10,max_provider_requests=1,max_tool_calls=0,token_counter=registration())


def test_continuation_is_recounted_after_tool_output(server):
    from prosaic_runtime import CustomTool
    from test_custom_tool_transport import call
    url, requests, responses=server
    responses.append(completion('',[call()]))
    seen=[]
    class Growing:
        def count_tokens(self, request):
            seen.append(request)
            return 20 if any(m['role']=='tool' for m in request['messages']) else 3
    from prosaic_runtime import TokenCounterRegistration
    reg=TokenCounterRegistration(registration().descriptor,Growing())
    b=ExecutionBudget(timeout_s=10,max_provider_requests=5,max_tool_calls=2,max_reported_tokens=20,token_counter=reg)
    tool=CustomTool('lookup_catalog','Lookup',{'type':'object','properties':{'sku':{'type':'string'}},'required':['sku'],'additionalProperties':False},
                    lambda args:{'data':'x'*100},'v1')
    from prosaic_runtime import ProsaicRuntime, RuntimeConfig
    config=RuntimeConfig({'local':EndpointConfig(url,'test',max_tokens=5,features={'streaming':False})},
                         {'fast':'local'},'local',frozenset({'lookup_catalog'}))
    with b.activate():
        result=ProsaicRuntime(config,custom_tools={'lookup_catalog':tool}).run(artifact(['lookup_catalog']),
                        policy=RunPolicy(allowed_tools=frozenset({'lookup_catalog'})))
    assert result.metadata['failure_reason']=='token_limit' and len(requests)==1
    assert len(seen)==2 and b.reported_tokens==7 and b.reserved_tokens==0


def test_invalid_counter_never_creates_accounting_intent(server):
    from prosaic_runtime import TokenCounterRegistration, ExecutionContext
    from prosaic_runtime.accounting import MemoryRecorder
    class Invalid:
        def count_tokens(self, request):return True
    url, requests, responses=server
    recorder=MemoryRecorder()
    b=ExecutionBudget(timeout_s=10,max_provider_requests=1,max_tool_calls=0,max_reported_tokens=20,
        token_counter=TokenCounterRegistration(registration().descriptor,Invalid()))
    context=ExecutionContext(application_id='app',tenant_id='t',billing_account_id='p',actor_id='a')
    with b.activate():
        result=runtime(EndpointConfig(url,'test',max_tokens=5)).run(artifact(),accounting=recorder,context=context)
    assert result.metadata['failure_reason']=='token_admission' and not recorder.intents and not requests


def test_transport_failure_retains_reservation_and_unknown_invocation_evidence():
    b=budget()
    with b.activate():
        result=runtime(EndpointConfig('http://127.0.0.1:1/v1','test',max_tokens=5,features={'streaming':False})).run(artifact())
    assert result.exit_code!=0 and result.token_usage is None
    assert b.reserved_tokens==8 and not b.usage_complete
    assert result.metadata['invocation_budgets_v1']['usage_complete'] is False


def test_anthropic_native_admission_uses_final_messages_request(native_server):
    from test_anthropic import response
    url, requests, responses=native_server
    responses.append(response())
    b=ExecutionBudget(timeout_s=10,max_provider_requests=1,max_tool_calls=0,max_reported_tokens=8,
        token_counter=registration(provider='anthropic',model='claude-test'))
    with b.activate():
        result=runtime(EndpointConfig(url,'claude-test',provider='anthropic',max_tokens=5,
                                      features={'streaming':False})).run(artifact())
    assert result.exit_code==0 and result.token_usage==7 and b.reserved_tokens==0
    assert len(requests)==1 and requests[0]['body']['max_tokens']==5
    assert result.metadata['token_admission_v1']==[b.token_admission]


def test_streaming_usage_reconciles_one_reservation(server):
    url, requests, responses=server
    parts=[{'choices':[{'delta':{'content':'yes'},'finish_reason':'stop'}]},
           {'choices':[],'usage':{'prompt_tokens':5,'completion_tokens':2,'total_tokens':7}}]
    body=''.join('data: '+json.dumps(p)+'\n\n' for p in parts)+'data: [DONE]\n\n'
    responses.append(('text/event-stream',body.encode()))
    b=budget(8)
    with b.activate():result=runtime(EndpointConfig(url,'test',max_tokens=5)).run(artifact())
    assert result.exit_code==0 and result.token_usage==7 and b.reported_tokens==7 and b.reserved_tokens==0


def test_adapter_retries_share_token_admission_remainder(server):
    from prosaic_runtime import RuntimeAdapter
    url, requests, responses=server
    responses.append(completion('first'))
    def retry(agent,arguments,*,execute,**options):
        execute(arguments)
        return execute(arguments)
    adapter=RuntimeAdapter(runtime(EndpointConfig(url,'test',max_tokens=5,features={'streaming':False})),'fixture',retry)
    b=budget(14)
    with b.activate():result=adapter.run(artifact())
    assert result.exit_code!=0 and result.metadata['failure_reason']=='token_limit'
    assert len(requests)==1 and b.reported_tokens==7 and b.provider_requests==1
