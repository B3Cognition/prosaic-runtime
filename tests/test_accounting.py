"""Financial attribution and capture, independent from model output admission."""
import json
from dataclasses import asdict
import pytest
from prosaic_runtime.accounting import ExecutionContext, ResolvedContext, MemoryRecorder, RateCard, normalize_usage, AccountingError
from prosaic_runtime import EndpointConfig, RunPolicy
from test_runtime import server, artifact, runtime, completion


def test_partial_ids_do_not_inherit_unrelated_payer():
    resolved = ExecutionContext(tenant_id='tenant-b').resolve(ExecutionContext(tenant_id='tenant-a', billing_account_id='payer-a'))
    assert resolved.billing_account_id == 'default'
    assert resolved.sources == ('default', 'provided', 'default')
    assert ResolvedContext.from_dict(resolved.to_dict()) == resolved


def test_application_scope_does_not_inherit_another_products_default():
    resolved=ExecutionContext(application_id='other').resolve(ExecutionContext(
        application_id='studio',tenant_id='tenant-a',billing_account_id='payer-a'))
    assert resolved.tenant_id==resolved.billing_account_id=='default'


def test_required_resolved_ids_cannot_be_null():
    with pytest.raises(ValueError):
        ResolvedContext(None,'default','default','req','inv')


def test_nontext_usage_is_not_priced_as_text():
    usage=normalize_usage({'prompt_tokens':5,'completion_tokens':2,'total_tokens':7,
        'prompt_tokens_details':{'cached_tokens':0,'audio_tokens':3}})
    assert usage['status']=='unsupported'
    assert RateCard('v1','test','2','8').assess({'requested_model':'test','usage':usage})['amount'] is None


def test_standard_text_response_zero_modality_and_prediction_details():
    usage=normalize_usage({'prompt_tokens':5,'completion_tokens':2,'total_tokens':7,
        'prompt_tokens_details':{'cached_tokens':0,'audio_tokens':0},
        'completion_tokens_details':{'reasoning_tokens':0,'audio_tokens':0,
                                     'accepted_prediction_tokens':0,'rejected_prediction_tokens':0}})
    assert usage['status']=='reported'
    assert RateCard('v1','test','2','8').assess({'response_model':'test','usage':usage})['status']=='estimated'


def test_requested_alias_alone_is_not_actual_model_evidence():
    usage=normalize_usage({'prompt_tokens':1000,'completion_tokens':200})
    assert RateCard('v1','test','2','8').assess({'requested_model':'test','usage':usage})['amount'] is None


@pytest.mark.parametrize('streaming',[False,True])
def test_duplicate_provider_usage_keys_are_quarantined(server,streaming):
    url,_,responses=server
    raw=b'{"model":"test","usage":{"prompt_tokens":1000,"prompt_tokens":1,"completion_tokens":2,"total_tokens":3},"choices":[{"message":{"content":"ok"},"finish_reason":"stop"}]}'
    if streaming:
        responses.append(('text/event-stream',b'data: '+raw+b'\n\ndata: [DONE]\n\n'))
    else:
        responses.append(('application/json',raw))
    recorder=MemoryRecorder()
    runtime(EndpointConfig(url,'test',features={'streaming':streaming}),accounting=recorder).run(artifact())
    assert next(iter(recorder.observations.values()))['usage']['status']=='untrusted'


def test_golden_decimal_and_reported_model():
    card = RateCard('v1', 'actual', '2', '8', '0.5')
    obs = {'response_model': 'actual', 'requested_model': 'alias', 'usage': normalize_usage({
        'prompt_tokens': 1000, 'completion_tokens': 200, 'total_tokens': 1200,
        'prompt_tokens_details': {'cached_tokens': 400}})}
    assert card.assess(obs)['amount'] == '0.003000000000000000'
    obs['response_model'] = 'unknown'
    assert card.assess(obs)['amount'] is None


@pytest.mark.parametrize('bad', [True, -1, 2**63, 1.2])
def test_usage_rejects_invalid_quantity(bad):
    assert normalize_usage({'total_tokens': bad})['status'] == 'untrusted'


@pytest.mark.parametrize('streaming', [False, True])
def test_one_call_attribution_detailed_usage_and_privacy(server, streaming):
    url, requests, responses = server
    usage = {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12,
             'prompt_tokens_details': {'cached_tokens': 4}, 'completion_tokens_details': {'reasoning_tokens': 1}}
    if streaming:
        chunks = [{'id': 'provider-1', 'model': 'actual', 'usage': None, 'choices': [{'delta': {'content': 'private response'}, 'finish_reason': 'stop'}]},
                  {'usage': usage, 'choices': []}, {'usage': usage, 'choices': []}]
        responses.append(('text/event-stream', (''.join('data: '+json.dumps(c)+'\n\n' for c in chunks)+'data: [DONE]\n\n').encode()))
    else:
        responses.append(('application/json', json.dumps({'id': 'provider-1', 'model': 'actual', 'usage': usage,
            'choices': [{'message': {'content': 'private response'}, 'finish_reason': 'stop'}]}).encode()))
    recorder = MemoryRecorder()
    result = runtime(EndpointConfig(url, 'alias', features={'streaming': streaming}), accounting=recorder).run(
        artifact(), 'private prompt', context=ExecutionContext(tenant_id='acme'))
    assert result.exit_code == 0
    assert len(recorder.intents) == len(recorder.observations) == 1
    intent = next(iter(recorder.intents.values()))
    observation = next(iter(recorder.observations.values()))
    assert intent['context']['tenant_id'] == 'acme'
    assert intent['context']['billing_account_id'] == 'default'
    assert observation['response_model'] == 'actual'
    assert observation['usage']['cached_input_tokens'] == 4
    assert observation['provider_request_id'] == 'provider-1'
    assert observation['usage']['total_tokens'] == 12
    stored = json.dumps([intent, observation])
    assert 'private prompt' not in stored and 'private response' not in stored
    assert 'acme' not in json.dumps(requests)
    assert set(asdict(result)) == {'exit_code','stdout','stderr','token_usage','cost_usd','timed_out','metadata'}


def test_intent_failure_prevents_network(server):
    url, requests, _ = server
    class Broken(MemoryRecorder):
        def prepare(self, intent):
            raise AccountingError('unavailable')
    result = runtime(EndpointConfig(url,'test'), accounting=Broken()).run(artifact())
    assert not requests
    assert result.metadata['failure_reason'] == 'accounting_failed'


def test_cancel_after_usage_retains_observation(server):
    url, _, responses = server
    chunks = [{'model':'test', 'usage': {'prompt_tokens': 5, 'completion_tokens': 2, 'total_tokens': 7},
               'choices':[{'delta':{'content':'done'},'finish_reason':'stop'}]}]
    responses.append(('text/event-stream', (''.join('data: '+json.dumps(c)+'\n\n' for c in chunks)+'data: [DONE]\n\n').encode()))
    recorder, events = MemoryRecorder(), []
    result = runtime(EndpointConfig(url,'test'), accounting=recorder).run(artifact(), on_event=events.append,
        cancelled=lambda: any(e['event']=='text_delta' for e in events))
    assert result.exit_code == 130
    assert next(iter(recorder.observations.values()))['usage']['total_tokens'] == 7
    assert len(result.metadata['accounting_v1']['provider_call_ids'])==1


def test_failed_final_write_does_not_retry_model(server):
    url,requests,responses=server
    responses.append(completion())
    class Broken(MemoryRecorder):
        def observe(self,call_id,observation):
            raise AccountingError('failed')
    recorder=Broken()
    result=runtime(EndpointConfig(url,'test',features={'streaming':False}),accounting=recorder).run(artifact())
    assert result.metadata['failure_reason']=='accounting_failed'
    assert len(requests)==len(recorder.intents)==1
    assert len(result.metadata['accounting_v1']['provider_call_ids'])==1


def test_conflicting_stream_snapshots_are_quarantined(server):
    url,_,responses=server
    chunks=[{'choices':[{'delta':{'content':'ok'},'finish_reason':'stop'}]},
        {'usage':{'prompt_tokens':5,'completion_tokens':2,'total_tokens':7}},
        {'usage':{'prompt_tokens':5,'completion_tokens':3,'total_tokens':8}}]
    responses.append(('text/event-stream',(''.join('data: '+json.dumps(c)+'\n\n' for c in chunks)+'data: [DONE]\n\n').encode()))
    recorder=MemoryRecorder()
    runtime(EndpointConfig(url,'test'),accounting=recorder).run(artifact())
    assert next(iter(recorder.observations.values()))['usage']['status']=='untrusted'


def test_tool_loop_records_each_paid_turn(server,tmp_path):
    url, _, responses = server
    (tmp_path/'evidence').write_text('data')
    responses.extend([completion('', [{'id':'r','type':'function','function':{'name':'read_file','arguments':'{"path":"evidence"}'}}]),completion()])
    recorder=MemoryRecorder()
    result=runtime(EndpointConfig(url,'test',features={'streaming':False}),accounting=recorder).run(
        artifact('read'),cwd=tmp_path,policy=RunPolicy(allowed_tools=frozenset({'read_file'}),read_roots=('.',)))
    assert result.token_usage == 14
    assert len(recorder.observations)==2
    assert sum(o['usage']['total_tokens'] for o in recorder.observations.values())==14
