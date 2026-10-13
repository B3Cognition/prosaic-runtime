"""A host adapter cannot reset allowances or erase incurred attempt evidence."""
from dataclasses import replace
import pytest
import prosaic_runtime as api
from prosaic_runtime import Result, RunPolicy, ProsaicRuntime
from test_runtime import artifact, runtime, server


def adapter(delegate, invoke, **kwargs):
    assert hasattr(api, 'RuntimeAdapter'), 'missing public bounded RuntimeAdapter'
    return api.RuntimeAdapter(delegate, version='test-v1', invoke=invoke, **kwargs)


def test_adapter_accumulates_usage_and_reduces_retry_allowances(monkeypatch):
    policies = []
    results = iter([Result(1, '', '', token_usage=15), Result(0, 'raw', '', token_usage=20)])
    def run(self, agent, arguments, *, policy, **options):
        policies.append(policy)
        result = next(results)
        result.metadata['invocation_budgets_v1'] = {'provider_requests': 1, 'tool_calls': 0, 'reported_tokens': result.token_usage, 'usage_complete': True}
        return result
    monkeypatch.setattr(ProsaicRuntime, 'run', run)
    def invoke(agent, arguments, *, execute, **options):
        execute('first')
        return replace(execute('second'), stdout='normalized')
    result = adapter(runtime(), invoke).run(artifact(), 'host', policy=RunPolicy(max_provider_requests=2, max_tool_calls=0, max_reported_tokens=40))
    assert result.stdout == 'normalized' and result.token_usage == 35
    assert [p.max_provider_requests for p in policies] == [2, 1]
    assert [p.max_reported_tokens for p in policies] == [40, 25]
    assert result.metadata['invocation_budgets_v1']['provider_requests'] == 2
    assert [a['token_usage'] for a in result.metadata['runtime_adapter_v1']['attempts']] == [15, 20]


def test_adapter_unknown_usage_blocks_another_attempt(monkeypatch):
    calls = []
    def run(self, *args, **options):
        calls.append(options)
        return Result(1, '', '', metadata={'invocation_budgets_v1': {'provider_requests': 1, 'tool_calls': 0, 'reported_tokens': 0, 'usage_complete': False}})
    monkeypatch.setattr(ProsaicRuntime, 'run', run)
    def invoke(agent, arguments, *, execute, **options):
        execute('first')
        return execute('second')
    result = adapter(runtime(), invoke).run(artifact(), policy=RunPolicy(max_reported_tokens=40))
    assert len(calls) == 1 and result.token_usage is None
    assert result.metadata['failure_reason'] == 'usage_unknown'


def test_adapter_keeps_evidence_after_callback_failure(monkeypatch):
    monkeypatch.setattr(ProsaicRuntime, 'run', lambda *args, **kwargs: Result(0, 'private', '', token_usage=7))
    def invoke(agent, arguments, *, execute, **options):
        execute('first')
        raise RuntimeError('secret-callback-error')
    result = adapter(runtime(), invoke).run(artifact())
    assert result.exit_code == 1 and result.stdout == '' and result.token_usage == 7
    assert result.metadata['failure_reason'] == 'adapter_callback_failed'
    assert 'secret' not in result.stderr


def test_adapter_preserves_cancelled_and_zero_request_boundaries(monkeypatch):
    calls = []
    monkeypatch.setattr(ProsaicRuntime, 'run', lambda *args, **kwargs: calls.append(args))
    def invoke(agent, arguments, *, execute, **options):
        return execute(arguments)
    result = adapter(runtime(), invoke).run(artifact(), cancelled=lambda: True)
    assert result.exit_code == 130 and calls == []
    result = adapter(runtime(), invoke).run(artifact(), policy=RunPolicy(max_provider_requests=0))
    assert result.metadata['failure_reason'] == 'provider_request_limit' and calls == []


def test_adapter_rejects_instance_run_override():
    delegate = runtime()
    delegate.run = lambda *args, **kwargs: Result(0, 'forged', '')
    with pytest.raises(ValueError, match='unmodified'):
        adapter(delegate, lambda *args, **kwargs: None)


def test_adapter_cannot_return_success_without_dispatch():
    result = adapter(runtime(), lambda *args, **kwargs: Result(0, 'forged', '')).run(artifact())
    assert result.exit_code == 1 and result.stdout == ''
    assert result.metadata['failure_reason'] == 'adapter_result_mismatch'


def test_adapter_refuses_broader_tool_grants_before_dispatch(monkeypatch):
    calls=[]
    monkeypatch.setattr(ProsaicRuntime, 'run', lambda *args, **kwargs: calls.append(args))
    def invoke(agent, arguments, *, execute, **options):
        return execute(arguments, allowed_tools={'write_file'})
    result = adapter(runtime(), invoke).run(artifact(), policy=RunPolicy())
    assert result.exit_code == 1 and calls == []


def test_adapter_exhausted_requests_keep_first_attempt_evidence(monkeypatch):
    calls=[]
    def run(self, *args, **kwargs):
        calls.append(kwargs)
        return Result(1, '', '', token_usage=5)
    monkeypatch.setattr(ProsaicRuntime, 'run', run)
    def invoke(agent, arguments, *, execute, **options):
        execute(arguments)
        return execute(arguments)
    result = adapter(runtime(), invoke).run(artifact(), policy=RunPolicy(max_provider_requests=1))
    assert len(calls) == 1 and result.token_usage == 5
    assert result.metadata['failure_reason'] == 'provider_request_limit'


def test_adapter_retains_known_partial_usage_when_total_is_unknown(monkeypatch):
    monkeypatch.setattr(ProsaicRuntime, 'run', lambda *args, **kwargs: Result(1, '', '', metadata={
        'invocation_budgets_v1': {'provider_requests': 2, 'tool_calls': 0, 'reported_tokens': 5, 'usage_complete': False}}))
    result = adapter(runtime(), lambda agent, arguments, execute, **options: execute(arguments)).run(artifact())
    assert result.token_usage is None
    assert result.metadata['invocation_budgets_v1']['reported_tokens'] == 5


def test_adapter_real_transport_preserves_events_and_projection(server):
    from test_runtime import completion
    from prosaic_runtime import EndpointConfig
    url, requests, responses = server
    responses.append(completion('raw'))
    events=[]
    def invoke(agent, arguments, *, execute, **options):
        return replace(execute('projected', allowed_tools=frozenset()), stdout='normalized')
    result=adapter(runtime(EndpointConfig(url,'small',features={'streaming':False})),invoke).run(
        artifact(), 'host', policy=RunPolicy(max_provider_requests=1,max_tool_calls=0,max_reported_tokens=10),on_event=events.append)
    assert result.stdout=='normalized' and result.token_usage==7
    assert len(requests)==1 and 'projected' in str(requests[0])
    assert events[0]['event']=='started' and events[-1]['event']=='completed'
