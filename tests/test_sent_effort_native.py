"""Actual launched argv and aggregate attempt evidence, without vendor calls."""
from dataclasses import replace
import json
import pytest
from prosaic_runtime import NativePolicy, NativeRuntime, NativeRuntimeConfig, NativeProfile, ProfilePool, RuntimeAdapter, Result, RunPolicy
from test_reasoning_effort import native_fixture, artifact, captured_effort
from test_unified_runtime import runtime as unified_runtime, policy as unified_policy
from test_prework_safety import rejection_server
from test_runtime import completion
from test_sent_effort import KEY, evidence, runner, agent, wire


@pytest.mark.parametrize('backend',['claude-code','codex-cli'])
@pytest.mark.parametrize('requested,default',[(None,None),(None,'medium'),('high','low')])
def test_native_actual_launch_reports_only_the_effort_argv(tmp_path,backend,requested,default):
    rt=native_fixture(tmp_path,backend,default=default)
    result=rt.run(artifact(requested),cwd=tmp_path,policy=NativePolicy())
    actual=captured_effort(result,backend);row=evidence(result,1)[0]
    assert row['transport']=='process' and row['backend']==backend and row['profile']=='native' and row['model']=='example'
    assert row['provider_call_id'] is None and row['requested_effort']==requested and row['configured_effort']==default
    expected=['--effort',actual] if backend=='claude-code' and actual else ['-c','model_reasoning_effort='+json.dumps(actual)] if actual else []
    assert row['sent']=={'argv':expected}
    assert row['format']==(('claude_cli' if backend=='claude-code' else 'codex_cli') if actual else 'omitted')
    assert str(tmp_path) not in json.dumps(result.metadata[KEY])


@pytest.mark.parametrize('mode',['missing','cancel','unsupported'])
def test_native_no_launch_is_zero_not_a_sent_attempt(tmp_path,mode):
    rt=native_fixture(tmp_path,'claude-code',default='low')
    if mode=='missing':
        p=replace(rt.config.profiles['native'],executable=str(tmp_path/'absent'))
        rt=NativeRuntime(NativeRuntimeConfig({'native':p},default_profile='native'))
    if mode=='unsupported':
        with pytest.raises(ValueError,match='effort is unavailable'):
            rt.run(artifact('none'),cwd=tmp_path,policy=NativePolicy())
        return
    out=rt.run(artifact('high'),cwd=tmp_path,policy=NativePolicy(),cancelled=(lambda:True) if mode=='cancel' else None)
    assert out.exit_code!=0;evidence(out,0)


def test_native_rows_observe_command_not_selected_profile(tmp_path,monkeypatch):
    import prosaic_runtime.native_backends as nb
    original=nb.native_command
    def changed(*args,**kwargs):
        argv=original(*args,**kwargs);argv[argv.index('--effort')+1]='low';return argv
    monkeypatch.setattr(nb,'native_command',changed)
    result=native_fixture(tmp_path,'claude-code',default='medium').run(artifact('high'),cwd=tmp_path,policy=NativePolicy())
    assert evidence(result,1)[0]['sent']=={'argv':['--effort','low']}


def test_unified_fallback_preserves_every_launched_attempt_and_default(rejection_server,tmp_path):
    url,requests,responses=rejection_server;responses.append((200,completion('done')[1],0))
    rt=unified_runtime(tmp_path,url)
    native=replace(rt.config.native_config,profiles={name:replace(p,effort='low') for name,p in rt.config.native_config.profiles.items()})
    rt.config=replace(rt.config,native_config=native)
    out=rt.run(artifact('high'),cwd=tmp_path,policy=unified_policy())
    assert out.exit_code==0
    rows=evidence(out,3)
    assert [r['backend'] for r in rows]==['claude-code','codex-cli','openai-compatible']
    assert [r['configured_effort'] for r in rows[:2]]==['low','low']
    assert [r['requested_effort'] for r in rows]==['high']*3
    assert rows[2]['sent']=={'reasoning_effort':'high'}
    responses.append((200,completion('again')[1],0))
    again=rt.run(artifact('high'),cwd=tmp_path,policy=unified_policy());assert len(evidence(again,1))==1


def test_unified_exception_is_unknown_not_zero(rejection_server,tmp_path,monkeypatch):
    import prosaic_runtime.unified as u
    def fail(*a,**k):raise RuntimeError('PRIVATE_DELEGATE')
    monkeypatch.setattr(u.NativeRuntime,'run',fail)
    url,_,_=rejection_server;out=unified_runtime(tmp_path,url).run(artifact(),cwd=tmp_path,policy=unified_policy())
    d=out.metadata[KEY];assert d['attempt_count'] is None and not d['complete'] and d['omitted_attempts'] is None
    assert not d['attempts'] and 'PRIVATE' not in json.dumps(d)


def test_adapter_owns_all_rows_despite_hook_fabrication_and_mutation(wire):
    fx,_=wire;fx.queue_text('one');fx.queue_text('two')
    def invoke(a,args,*,execute,**options):
        first=execute('first');first.metadata[KEY]['attempts'].clear()
        second=execute('second');second.metadata[KEY]={'fabricated':'PRIVATE'}
        return second
    out=RuntimeAdapter(runner(fx,default='high' if fx.provider=='openai-compatible' else None),version='v1',invoke=invoke).run(agent(fx),policy=RunPolicy(max_provider_requests=2,max_tool_calls=0))
    rows=evidence(out,2);assert len({r['provider_call_id'] for r in rows})==2


def test_adapter_without_execution_rejects_fake_sent_history(wire):
    fx,_=wire
    def invoke(*a,**k):return Result(1,'','',metadata={KEY:{'fabricated':True}})
    out=RuntimeAdapter(runner(fx),version='v1',invoke=invoke).run(agent(fx))
    evidence(out,0);assert not fx.requests


def test_pool_exposes_sent_effort_capability():
    from prosaic_runtime import UnifiedRuntime
    assert KEY in UnifiedRuntime.capabilities and KEY in NativeRuntime.capabilities


@pytest.mark.parametrize('backend,argv',[
    ('claude-code',['fake','--effort','PRIVATE']),
    ('claude-code',['fake','--effort','low','--effort','high']),
    ('claude-code',['fake','--effort']),
    ('codex-cli',['fake','-c','model_reasoning_effort="PRIVATE"']),
    ('codex-cli',['fake','model_reasoning_effort="high"']),
    ('codex-cli',['fake','-c','model_reasoning_effort= "high"']),
])
def test_malformed_native_effort_is_unavailable_and_private(backend,argv):
    from prosaic_runtime.effort_evidence import EffortTrace,native_attempt
    t=EffortTrace();t.add(native_attempt(argv,backend=backend,profile='native',model='example',requested='high',configured=None));d=t.to_dict()
    assert d['attempt_count']==1 and d['complete'] is False and d['attempts'][0]['format']=='unavailable'
    assert d['attempts'][0]['sent']=={} and 'PRIVATE' not in json.dumps(d)


def test_adapter_unknown_delegate_after_known_attempt_keeps_previous_rows(wire,monkeypatch):
    fx,_=wire;rt=runner(fx);fx.queue_text('one');original=type(rt).run;n=[0]
    def delegate(self,*a,**kw):
        n[0]+=1
        if n[0]>1:raise RuntimeError('PRIVATE_DELEGATE')
        return original(self,*a,**kw)
    monkeypatch.setattr(type(rt),'run',delegate)
    def invoke(a,args,*,execute,**options):execute('one');return execute('two')
    out=RuntimeAdapter(rt,version='v1',invoke=invoke).run(agent(fx),policy=RunPolicy(max_provider_requests=2,max_tool_calls=0))
    d=out.metadata[KEY]
    assert out.exit_code!=0 and d['attempt_count'] is None and d['omitted_attempts'] is None and not d['complete']
    assert len(d['attempts'])==1 and len(fx.requests)==1


@pytest.mark.parametrize('backend,level',[('claude-code','max'),('codex-cli','ultra')])
def test_metadata_discovery_processes_are_excluded(tmp_path,backend,level):
    from test_effort_discovery import metadata_fixture
    profile,trace=metadata_fixture(tmp_path,backend,['low',level])
    rt=NativeRuntime(NativeRuntimeConfig({'native':profile},default_profile='native'))
    out=rt.run(artifact(level),cwd=tmp_path,policy=NativePolicy())
    assert out.exit_code==0 and trace.read_text().splitlines()==['probe']
    row=evidence(out,1)[0];assert row['requested_effort']==level and row['configured_effort'] is None


@pytest.mark.parametrize('effects',[False,True])
def test_native_only_pool_tracks_fallback_and_failure_after_launch(tmp_path,effects):
    from test_native_runtime import setup_runtime
    rt=setup_runtime(tmp_path,effects=effects)
    out=rt.run(artifact('high'),cwd=tmp_path,policy=NativePolicy())
    rows=evidence(out,1 if effects else 2)
    assert [r['profile'] for r in rows]==(['primary'] if effects else ['primary','fallback'])
    assert all(r['sent']=={'argv':['-c','model_reasoning_effort="high"']} for r in rows)


@pytest.mark.parametrize('second',['api','native'])
def test_api_prework_failure_joins_next_api_or_native_attempt(rejection_server,tmp_path,second):
    from prosaic_runtime import UnifiedRuntime,ProfilePool
    url,requests,responses=rejection_server
    responses.append((429,{'error':{'code':'rate_limit_exceeded'}},0))
    rt=unified_runtime(tmp_path,url,order=('local','codex'),native_error='success')
    if second=='api':
        responses.append((200,completion('done')[1],0))
        api=replace(rt.config.api_config,profiles={**rt.config.api_config.profiles,'second':rt.config.api_config.profiles['local']})
        rt=UnifiedRuntime(replace(rt.config,api_config=api,pools={'agents':ProfilePool(('local','second'))},api_quota_keys={'local':'l','second':'s'}))
    out=rt.run(artifact('high'),cwd=tmp_path,policy=unified_policy())
    assert out.exit_code==0
    rows=evidence(out,2)
    assert [r['transport'] for r in rows]==['http','http' if second=='api' else 'process']
    assert len(requests)==(2 if second=='api' else 1)


@pytest.mark.parametrize('discovered',[False,True])
def test_unified_artifact_override_does_not_revalidate_unused_native_default(tmp_path,discovered):
    from prosaic_runtime import UnifiedRuntime,UnifiedRuntimeConfig,PoolPolicy
    if discovered:
        from test_effort_discovery import metadata_fixture
        profile,_=metadata_fixture(tmp_path,'codex-cli',['high','ultra'])
        native=NativeRuntimeConfig({'native':replace(profile,effort='max')},default_profile='native')
        requested,default='ultra','max'
    else:
        native=native_fixture(tmp_path,'codex-cli',default='ultra').config
        requested,default='high','ultra'
    direct=NativeRuntime(native).run(artifact(requested),cwd=tmp_path,policy=NativePolicy())
    assert direct.exit_code==0
    config=UnifiedRuntimeConfig(native,None,{'pool':ProfilePool(('native',))},'pool',{})
    result=UnifiedRuntime(config).run(artifact(requested),cwd=tmp_path,
        policy=PoolPolicy(timeout_s=5,max_attempts=1,max_native_attempts=1,native_policy=NativePolicy()))
    assert result.exit_code==0,result.stderr
    row=evidence(result,1)[0]
    assert row['requested_effort']==requested and row['configured_effort']==default
    assert row['sent']=={'argv':['-c','model_reasoning_effort='+json.dumps(requested)]}
