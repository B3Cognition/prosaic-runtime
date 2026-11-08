"""Result evidence describes real attempted wire controls, never provider compliance."""
from dataclasses import replace
import json
import socket
from concurrent.futures import ThreadPoolExecutor

import pytest

from prosaic_runtime import (EndpointConfig, ProsaicArtifact, ProsaicRuntime, RuntimeConfig,
                            RunPolicy, TurnLimits, generic_guards, CustomTool)
from prosaic_runtime.accounting import MemoryRecorder, ExecutionContext
from support.conversation_server import ConversationFixture

KEY = 'reasoning_effort_v1'
GEMMA = 'gemma-4-31B-it-oQ4e-mtp'
QWEN = 'ThinkingCap-Qwen3.8-27B-MLX-4bit-DWQ'


def runner(fx, *, model='example', default=None, extra=None, **kwargs):
    endpoint = replace(fx.runtime.config.profiles['local'], model=model,
        supported_efforts=('none','low','medium','high','xhigh','max') if fx.provider=='openai-compatible' else None,
        features={'streaming':fx.streaming, 'reasoning_effort':default, **(extra or {})})
    return ProsaicRuntime(replace(fx.runtime.config,profiles={'local':endpoint}),
        source='/private/tmp/sent-effort-golden-project/.prosaic', **kwargs)


def agent(fx, effort=None):
    a=fx.agent
    return replace(a, frontmatter={**a.frontmatter, **({'effort':effort} if effort is not None else {})})


def invoke(rt, fx, a, entry='run', **kwargs):
    if entry=='run':return rt.run(a,policy=fx.policy,**kwargs)
    prepared=rt.prepare_turn(a,message='Review',policy=fx.policy)
    return rt.turn(prepared if entry=='prepared' else a,
        **({} if entry=='prepared' else {'message':'Review','policy':fx.policy}),**kwargs).result


@pytest.fixture(params=['openai-compatible','anthropic'])
def wire(request,monkeypatch):
    monkeypatch.setenv('no_proxy','*')
    with ConversationFixture(request.param) as fx:
        headers=[];original=fx.http.RequestHandlerClass.do_POST
        def capture(handler):
            headers.append({k.lower():v for k,v in handler.headers.items()});return original(handler)
        monkeypatch.setattr(fx.http.RequestHandlerClass,'do_POST',capture)
        yield fx,headers


def evidence(result, count):
    trace=result.metadata[KEY]
    assert set(trace)=={'version','attempt_count','complete','omitted_attempts','attempts'}
    assert trace['version']==1 and trace['complete'] is True
    assert trace['attempt_count']==count and trace['omitted_attempts']==0
    assert len(trace['attempts'])==count
    assert [r['index'] for r in trace['attempts']]==list(range(1,count+1))
    for row in trace['attempts']:
        assert set(row)=={'index','transport','backend','profile','model','provider_call_id',
                         'requested_effort','configured_effort','format','sent','provider_confirmation'}
        assert row['provider_confirmation']=='not_observed'
    return trace['attempts']


def test_capability_is_public():
    assert KEY in ProsaicRuntime.capabilities


@pytest.mark.parametrize('entry',['run','turn','prepared'])
@pytest.mark.parametrize('model,requested,default,extra,fmt,sent',[
    ('example','high','low',{},'reasoning_effort',{'reasoning_effort':'high'}),
    ('example',None,'medium',{},'reasoning_effort',{'reasoning_effort':'medium'}),
    ('example','none','low',{},'reasoning_effort',{'reasoning_effort':'none'}),
    ('example',None,None,{},'omitted',{}),
    (GEMMA,'high','low',{},'thinking_toggle',{'chat_template_kwargs':{'enable_thinking':True}}),
    (GEMMA,'none','low',{},'thinking_toggle',{'chat_template_kwargs':{'enable_thinking':False}}),
    (QWEN,'xhigh','low',{},'chat_template_effort',{'chat_template_kwargs':{'enable_thinking':True},'reasoning_effort':'xhigh'}),
    (QWEN,'none','low',{},'thinking_toggle',{'chat_template_kwargs':{'enable_thinking':False}}),
    ('custom','medium','high',{'reasoning_effort_format':'thinking_toggle'},'thinking_toggle',{'chat_template_kwargs':{'enable_thinking':True}}),
])
def test_exact_actual_http_controls_not_inferred_from_requested_tier(tmp_path,monkeypatch,entry,model,requested,default,extra,fmt,sent):
    monkeypatch.setenv('no_proxy','*')
    with ConversationFixture('openai-compatible') as fx:
        headers=[];old=fx.http.RequestHandlerClass.do_POST
        def capture(h):headers.append({k.lower():v for k,v in h.headers.items()});return old(h)
        monkeypatch.setattr(fx.http.RequestHandlerClass,'do_POST',capture)
        rt=runner(fx,model=model,default=default,extra=extra);a=agent(fx,requested)
        prepared=rt.prepare_turn(a,message='Review',policy=fx.policy);identity=prepared.context_identity
        fx.queue_text('done');result=invoke(rt,fx,a,entry)
        assert result.exit_code==0,result.stderr
        row=evidence(result,1)[0]
        assert row['sent']==sent=={k:v for k,v in fx.requests[0].items() if k in ('reasoning_effort','chat_template_kwargs')}
        assert row['format']==fmt and row['requested_effort']==requested and row['configured_effort']==default
        assert row['backend']=='openai-compatible' and row['model']==model and row['profile']=='local' and row['transport']=='http'
        assert row['provider_call_id']==headers[0]['x-client-request-id']==headers[0]['x-correlation-id']
        assert rt.prepare_turn(a,message='Review',policy=fx.policy).context_identity==identity


@pytest.mark.parametrize('streaming',[False,True])
@pytest.mark.parametrize('entry',['run','turn','prepared'])
def test_both_wire_omission_and_streaming_are_observed(wire,streaming,entry):
    fx,headers=wire
    if streaming:fx.queue_stream_text('done')
    else:fx.queue_text('done')
    rt=runner(fx);result=invoke(rt,fx,agent(fx),entry)
    assert result.exit_code==0,result.stderr
    row=evidence(result,1)[0]
    assert row['sent']=={} and row['format']=='omitted' and row['backend']==fx.provider
    assert row['provider_call_id']==headers[0]['x-client-request-id']
    assert 'provider-private-secret' not in json.dumps(result.metadata[KEY])


@pytest.mark.parametrize('kind',['tool','repair'])
def test_tools_and_guard_repairs_retain_all_call_rows_and_ledger_ids(wire,kind):
    fx,headers=wire;rec=MemoryRecorder(provider_id=fx.provider)
    effort='high' if fx.provider=='openai-compatible' else None
    if kind=='tool':
        fx.add_tool('lookup',lambda args:{'found':True});a=agent(fx,effort)
        fx.queue_calls([{'id':'one','name':'lookup','arguments':'{}'}]);fx.queue_text('done')
    else:
        a=ProsaicArtifact('rules','subagent',{'tools':[],**({'effort':effort} if effort else {})},
            '- ALWAYS answer JSON. <!-- enforce: strict_json(stage=final, on_violation=retry) -->\n')
        fx.queue_text('bad');fx.queue_text('{}')
    rt=runner(fx,accounting=rec,custom_tools=fx.tools,guards=generic_guards())
    result=invoke(rt,fx,a,'turn',context=ExecutionContext(request_metadata={'variant':'TRACE_SECRET_NOT_ON_WIRE'}))
    assert result.exit_code==0 and len(fx.requests)==2
    rows=evidence(result,2)
    assert [x['provider_call_id'] for x in rows]==[h['x-client-request-id'] for h in headers]==list(rec.intents)
    for row,body in zip(rows,fx.requests):
        assert row['sent']=={k:v for k,v in body.items() if k in ('reasoning_effort','chat_template_kwargs')}
    assert 'TRACE_SECRET_NOT_ON_WIRE' not in json.dumps(rows)+json.dumps(fx.requests)


@pytest.mark.parametrize('when',['prepare','observe','cancel','key'])
def test_early_and_late_failure_do_not_fabricate_or_lose_dispatch(wire,tmp_path,when):
    fx,_=wire
    class Recorder(MemoryRecorder):
        def prepare(self,intent):
            if when=='prepare':raise RuntimeError('PRIVATE_PREPARE')
            super().prepare(intent)
        def observe(self,*args):
            if when=='observe':raise RuntimeError('PRIVATE_OBSERVE')
            return super().observe(*args)
    rec=Recorder(provider_id=fx.provider);rt=runner(fx,accounting=rec)
    if when=='key':rt.config=replace(rt.config,profiles={'local':replace(rt.config.profiles['local'],api_key_file=str(tmp_path/'absent-key'))})
    fx.queue_text('done');result=invoke(rt,fx,agent(fx),cancelled=(lambda:True) if when=='cancel' else None)
    assert result.exit_code!=0
    rows=evidence(result,1 if when=='observe' else 0)
    assert len(fx.requests)==len(rows)
    assert 'PRIVATE_' not in json.dumps(result.metadata[KEY])


def test_connection_refusal_reports_attempt_not_provider_confirmation():
    with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    rt=ProsaicRuntime(RuntimeConfig({'local':EndpointConfig(f'http://127.0.0.1:{port}/v1','example',features={'streaming':False,'reasoning_effort':'high'})},{},'local'))
    a=ProsaicArtifact('a','subagent',{'tools':[]},'Answer.')
    result=rt.run(a,policy=RunPolicy(timeout_s=2,max_provider_requests=1,max_tool_calls=0))
    assert result.exit_code!=0
    row=evidence(result,1)[0];assert row['sent']=={'reasoning_effort':'high'} and row['provider_confirmation']=='not_observed'
    assert result.metadata['invocation_budgets_v1']['provider_requests']==1


@pytest.mark.parametrize('status',[401,429,503])
def test_http_failures_keep_actual_sent_controls(wire,status):
    fx,_=wire;fx.queue_http_failure(status)
    result=invoke(runner(fx),fx,agent(fx));assert result.exit_code!=0
    evidence(result,1)


def test_result_copy_and_concurrent_calls_do_not_alias_or_accumulate(monkeypatch):
    monkeypatch.setenv('no_proxy','*')
    with ConversationFixture('openai-compatible') as fx:
        rt=runner(fx,default='low');fx.queue_text('one');fx.queue_text('two')
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda e:rt.run(agent(fx,e)),['medium','high']))
        rows=[evidence(r,1)[0] for r in results]
        assert {r['sent']['reasoning_effort'] for r in rows}=={'medium','high'}
        assert len({r['provider_call_id'] for r in rows})==2
        rows[0]['sent']['reasoning_effort']='mutated';assert rows[1]['sent']['reasoning_effort']!='mutated'
        fx.queue_text('default');assert evidence(rt.run(agent(fx)),1)[0]['sent']=={'reasoning_effort':'low'}


def test_small_turn_metadata_cap_trims_only_observability(wire):
    fx,_=wire;fx.queue_text('done');rt=runner(fx)
    out=rt.turn(agent(fx),message='Review',limits=TurnLimits(max_metadata_bytes=420),policy=fx.policy)
    assert out.result.exit_code==0 and out.completed_turn is not None
    trace=out.result.metadata.get(KEY)
    if trace is not None:
        assert trace['attempt_count']==1 and trace['complete'] is False and trace['omitted_attempts']==1 and trace['attempts']==[]
    assert len(json.dumps(out.result.metadata,separators=(',',':')).encode())<=420


def test_public_0917_identity_goldens_are_unchanged():
    config=RuntimeConfig({'local':EndpointConfig('http://127.0.0.1:9/v1','example',features={'streaming':False,'reasoning_effort':'low'})},{},'local')
    rt=ProsaicRuntime(config,source='/private/tmp/sent-effort-golden-project/.prosaic',guards=generic_guards())
    for body,digest,identity,byte_count in [
        ('Review the supplied input.','1801cada2deae3d23c65dfbe2e994843ca098d8ca8c2478a35dae615863f90ec','211812e29feafbc4aff302042cf97df9a63c031019d97f8486fc95fee3075eba',260),
        ('- ALWAYS answer JSON. <!-- enforce: strict_json(stage=final, on_violation=retry) -->\n','393087330d44e9bce2e87271904f5bc268793a05f98668aaf68c04b546960a82','5814d7ade22c7d5ddfcee94045f2f06f91ade03dffc56b90ae4c5a060b72357c',257)]:
        a=ProsaicArtifact('approved','subagent',{'tools':[],'effort':'high'},body);p=rt.prepare_turn(a,message='Review',input_data={'value':'fixed'})
        assert a.digest==digest and p.context_identity==identity and p.input_bytes==byte_count


def test_pure_projection_retains_only_actual_allowlisted_values():
    from prosaic_runtime.effort_evidence import EffortTrace,http_attempt
    row=http_attempt(json.dumps({'model':'example','reasoning_effort':'high','messages':['PRIVATE_PROMPT'],
        'headers':{'Authorization':'PRIVATE_KEY'},'chat_template_kwargs':{'enable_thinking':True,'secret':'PRIVATE_OTHER'}}).encode(),
        backend='openai-compatible',profile='local',call_id='a'*32,requested='low',configured='medium')
    t=EffortTrace();t.add(row);d=t.to_dict();r=d['attempts'][0]
    assert r['format']=='chat_template_effort' and r['sent']=={'reasoning_effort':'high','chat_template_kwargs':{'enable_thinking':True}}
    assert r['requested_effort']=='low' and r['configured_effort']=='medium'
    assert 'PRIVATE_' not in json.dumps(d)
    r['sent'].clear();assert t.to_dict()['attempts'][0]['sent']


@pytest.mark.parametrize('payload',[b'not JSON',b'[]',b'{"reasoning_effort":"PRIVATE_SECRET"}',
    b'{"chat_template_kwargs":{"enable_thinking":1}}',b'{"reasoning_effort":"low","reasoning_effort":"high"}'])
def test_unknown_malformed_controls_are_unavailable_not_omitted(payload):
    from prosaic_runtime.effort_evidence import EffortTrace,http_attempt
    row=http_attempt(payload,backend='openai-compatible',profile='local',call_id='a'*32,requested=None,configured=None)
    t=EffortTrace();t.add(row);d=t.to_dict();assert d['complete'] is False
    assert d['attempts'][0]['format']=='unavailable' and d['attempts'][0]['sent']=={}
    assert 'PRIVATE_SECRET' not in json.dumps(d)


def test_bounded_collector_marks_omission_and_unknown_count_explicitly():
    from prosaic_runtime.effort_evidence import EffortTrace,http_attempt
    t=EffortTrace();row=http_attempt(b'{"model":"example","reasoning_effort":"high"}',backend='openai-compatible',profile='local',call_id='a'*32,requested='high',configured=None)
    for _ in range(80):t.add(row)
    d=t.to_dict();assert d['attempt_count']==80 and len(d['attempts'])<=64 and d['omitted_attempts']==80-len(d['attempts']) and not d['complete']
    assert len(json.dumps(d,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode())<=32768
    smaller=t.to_dict(max_bytes=250);assert smaller['attempt_count']==80 and smaller['attempts']==[] and smaller['omitted_attempts']==80
    copy=EffortTrace();copy.merge(d);copy.merge(None);unknown=copy.to_dict()
    assert unknown['complete'] is False and unknown['attempt_count'] is None and unknown['omitted_attempts'] is None
    assert unknown['attempts']==d['attempts']


def test_model_free_tool_records_zero_attempts(wire):
    fx,_=wire;fx.add_tool('lookup',lambda args:{'found':True})
    rt=runner(fx,custom_tools=fx.tools)
    result=rt.run_tool('lookup',{},policy=fx.policy)
    assert result.exit_code==0 and not fx.requests
    evidence(result,0)


@pytest.mark.parametrize('boundary',['provider_limit','cancel','callback'])
def test_late_boundaries_keep_first_attempt_without_inventing_second(wire,boundary):
    fx,_=wire;fx.add_tool('lookup',lambda args:{'found':True});fx.queue_calls([{'id':'one','name':'lookup','arguments':'{}'}]);fx.queue_text('done')
    rt=runner(fx,custom_tools=fx.tools)
    def event(ev):
        if boundary=='callback' and ev['event']=='tool_completed':raise RuntimeError('PRIVATE_CALLBACK')
    limits=replace(fx.policy,max_provider_requests=1) if boundary=='provider_limit' else fx.policy
    out=rt.turn(agent(fx),message='Review',policy=limits,on_event=event,
        cancelled=(lambda:bool(fx.requests)) if boundary=='cancel' else None)
    assert out.result.exit_code!=0 and len(fx.requests)==1
    evidence(out.result,1)
    assert 'PRIVATE_CALLBACK' not in json.dumps(out.result.metadata[KEY])


@pytest.mark.parametrize('mutation',[
    lambda d:d.update(version=True),lambda d:d.update(attempt_count=True),
    lambda d:d.update(complete=1),lambda d:d.update(omitted_attempts=9),
    lambda d:d['attempts'][0].update(sent={'Authorization':'PRIVATE_KEY'}),
    lambda d:d['attempts'][0].update(provider_confirmation='honored'),
    lambda d:d['attempts'][0].update(profile='PRIVATE\nSECRET'),
    lambda d:d['attempts'][0].update(index=2),lambda d:d.update(unrecognized='PRIVATE'),
])
def test_invalid_delegate_documents_are_unavailable_not_trusted(mutation):
    from prosaic_runtime.effort_evidence import EffortTrace,http_attempt
    t=EffortTrace();t.add(http_attempt(b'{"reasoning_effort":"low"}',backend='openai-compatible',profile='local',call_id='a'*32,requested=None,configured=None))
    d=t.to_dict();mutation(d);consumer=EffortTrace();consumer.merge(d);out=consumer.to_dict()
    assert out['attempt_count'] is None and out['omitted_attempts'] is None and not out['complete'] and not out['attempts']
    assert 'PRIVATE' not in json.dumps(out)


def test_late_actual_encoded_bytes_take_precedence_over_annotations():
    from prosaic_runtime.effort_evidence import http_attempt
    row=http_attempt(b'{"model":"actual","reasoning_effort":"none"}',backend='openai-compatible',profile='local',call_id='a'*32,requested='high',configured='medium')
    assert row['sent']=={'reasoning_effort':'none'} and row['model']=='actual'


@pytest.mark.parametrize('size',[128,180,250])
def test_tiny_turn_cap_does_not_fail_accepted_output(wire,size):
    fx,_=wire;fx.queue_text('done')
    out=runner(fx).turn(agent(fx),message='Review',limits=TurnLimits(max_metadata_bytes=size),policy=fx.policy)
    assert out.result.exit_code==0 and out.completed_turn is not None
    assert len(json.dumps(out.result.metadata,separators=(',',':')).encode())<=size if size>=2 else out.result.metadata=={}


def test_native_facade_default_annotation_remains_owned_and_byte_bounded():
    from prosaic_runtime.effort_evidence import EffortTrace,http_attempt
    source=EffortTrace()
    row=http_attempt(json.dumps({'model':'m'*256,'reasoning_effort':'high'}).encode(),backend='openai-compatible',profile='p'*256,call_id='a'*32,requested='high',configured=None)
    for _ in range(64):source.add(row)
    original=source.to_dict();aggregate=EffortTrace();aggregate.merge(original,_configured_effort='ultra');out=aggregate.to_dict()
    assert out['attempt_count']==64 and len(json.dumps(out,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode())<=32768
    assert all(r['configured_effort']=='ultra' and r['sent']=={'reasoning_effort':'high'} for r in out['attempts'])
    assert all(r['configured_effort'] is None for r in original['attempts'])
