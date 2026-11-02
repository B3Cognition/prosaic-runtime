"""Neutral effort projects to actual tiered or binary model controls."""
from dataclasses import replace

import pytest

from prosaic_runtime import EndpointConfig, RuntimeConfig, ProsaicRuntime, NativeProfile
from prosaic_runtime.effort import validate_effort
from prosaic_runtime.openai_compatible import OpenAICompatibleBackend
from prosaic_runtime.types import Invocation
from test_reasoning_effort import artifact
from test_runtime import server, completion

GEMMA = 'gemma-4-31B-it-oQ4e-mtp'
QWEN = 'ThinkingCap-Qwen3.8-27B-MLX-4bit-DWQ'


def test_none_is_a_canonical_effort():
    assert validate_effort('none') == 'none'


@pytest.mark.parametrize('effort', ['none', 'low', 'medium', 'high', 'xhigh', 'max'])
@pytest.mark.parametrize('method', ['run', 'turn', 'constrained'])
def test_gemma_effort_uses_native_thinking_switch(server, tmp_path, effort, method):
    url, requests, responses = server
    endpoint = EndpointConfig(url, GEMMA, features={'streaming': False,
        **({'reasoning_effort': effort} if method == 'constrained' else {})})
    responses.append(completion('done'))
    if method == 'constrained':
        result = OpenAICompatibleBackend(endpoint).run_constrained_prompt(
            Invocation(str(tmp_path), 'Return done.', {}, 10), model=GEMMA,
            screen_output=lambda b:b, max_input_bytes=1024, max_capture_bytes=1024)
    else:
        runtime = ProsaicRuntime(RuntimeConfig({'api': endpoint}, {}, 'api'))
        prose = replace(artifact(effort), body='Return done.')
        result = runtime.run(prose, cwd=tmp_path) if method == 'run' else runtime.turn(prose, message='Go').result
    assert result.exit_code == 0, result.stderr
    assert requests[0]['chat_template_kwargs'] == {'enable_thinking': effort != 'none'}
    assert 'reasoning_effort' not in requests[0]


@pytest.mark.parametrize('effort', ['none', 'low', 'medium', 'xhigh'])
def test_local_qwen_keeps_real_tiers_and_disables_none(server, tmp_path, effort):
    url, requests, responses = server
    runtime = ProsaicRuntime(RuntimeConfig({'api': EndpointConfig(url, QWEN,
        features={'streaming': False})}, {}, 'api'))
    responses.append(completion('done'))
    result = runtime.run(artifact(effort), cwd=tmp_path)
    assert result.exit_code == 0, result.stderr
    assert requests[0]['chat_template_kwargs'] == {'enable_thinking': effort != 'none'}
    assert requests[0].get('reasoning_effort') == (None if effort == 'none' else effort)


def test_omitting_effort_leaves_gemma_server_default(server, tmp_path):
    url, requests, responses = server
    runtime = ProsaicRuntime(RuntimeConfig({'api': EndpointConfig(url, GEMMA,
        features={'streaming': False})}, {}, 'api'))
    responses.append(completion('done'))
    assert runtime.run(artifact(), cwd=tmp_path).exit_code == 0
    assert 'chat_template_kwargs' not in requests[0]
    assert 'reasoning_effort' not in requests[0]


@pytest.mark.parametrize('model,effort', [(GEMMA,'ultra'), (QWEN,'max'), (QWEN,'high')])
def test_model_limits_fail_before_transport(server, tmp_path, model, effort):
    url, requests, responses = server
    runtime = ProsaicRuntime(RuntimeConfig({'api': EndpointConfig(url, model)}, {}, 'api'))
    with pytest.raises(ValueError, match='effort'):
        runtime.run(artifact(effort), cwd=tmp_path)
    assert requests == []


def test_custom_toggle_profile_overrides_default_from_markdown(server, tmp_path):
    url, requests, responses = server
    runtime = ProsaicRuntime(RuntimeConfig({'api': EndpointConfig(url, 'custom-alias',
        features={'streaming': False, 'reasoning_effort_format':'thinking_toggle',
                  'reasoning_effort':'high'})}, {}, 'api'))
    responses.append(completion('done'))
    assert runtime.run(artifact('none'), cwd=tmp_path).exit_code == 0
    assert requests[0]['chat_template_kwargs'] == {'enable_thinking':False}


@pytest.mark.parametrize('value', ['unknown', True, 42, []])
def test_bad_effort_format_is_rejected_at_configuration(value):
    with pytest.raises(ValueError, match='reasoning_effort_format'):
        EndpointConfig('http://localhost/v1', 'custom', features={'reasoning_effort_format':value})


@pytest.mark.parametrize('backend', ['claude-code','codex-cli'])
def test_native_adapters_do_not_invent_an_off_setting(backend):
    with pytest.raises(ValueError, match='effort'):
        NativeProfile(backend, 'example', 'account', effort='none')


def test_format_override_is_bound_to_selected_model(server, tmp_path):
    url, requests, responses = server
    responses.append(completion('done'))
    endpoint = EndpointConfig(url, 'custom-alias', features={
        'reasoning_effort_format':'thinking_toggle','reasoning_effort':'low'})
    result = OpenAICompatibleBackend(endpoint).run_constrained_prompt(
        Invocation(str(tmp_path),'Return done.',{},10),model='other-model',
        screen_output=lambda b:b,max_input_bytes=1024,max_capture_bytes=1024)
    assert result.exit_code == 0
    assert requests[0]['reasoning_effort'] == 'low'
    assert 'chat_template_kwargs' not in requests[0]


def test_anthropic_format_override_is_not_silently_ignored():
    with pytest.raises(ValueError, match='reasoning_effort_format'):
        EndpointConfig('http://localhost','gemma',provider='anthropic',
            features={'reasoning_effort_format':'thinking_toggle'})


@pytest.mark.parametrize('model,declared', [
    ('nvidia/nemotron-3.5-super-vl-120b-a12b',None),
    ('unknown-model',None), (GEMMA,()), (GEMMA,('low','high'))])
def test_canonical_none_requires_an_available_off_control(server, tmp_path, model, declared):
    url,requests,responses=server
    runtime=ProsaicRuntime(RuntimeConfig({'api':EndpointConfig(url,model,
        supported_efforts=declared)}, {},'api'))
    with pytest.raises(ValueError,match='effort'):
        runtime.run(artifact('none'),cwd=tmp_path)
    assert requests==[]


@pytest.mark.parametrize('method',['run','turn','constrained'])
def test_legacy_operator_none_default_stays_compatible(server,tmp_path,method):
    url,requests,responses=server
    endpoint=EndpointConfig(url,'custom-model',supported_efforts=(),
        features={'streaming':False,'reasoning_effort':'none'})
    responses.append(completion('done'))
    if method=='constrained':
        result=OpenAICompatibleBackend(endpoint).run_constrained_prompt(
            Invocation(str(tmp_path),'Return done.',{},10),model='custom-model',
            screen_output=lambda b:b,max_input_bytes=1024,max_capture_bytes=1024)
    else:
        runtime=ProsaicRuntime(RuntimeConfig({'api':endpoint},{},'api'))
        prose=replace(artifact(),body='Return done.')
        result=runtime.run(prose,cwd=tmp_path) if method=='run' else runtime.turn(prose,message='Go').result
    assert result.exit_code==0,result.stderr
    assert requests[0]['reasoning_effort']=='none'


@pytest.mark.parametrize('model',[QWEN,'qwen3.8-max','deepseek-v4-pro'])
def test_model_override_uses_targets_off_capability(server,tmp_path,model):
    url,requests,responses=server
    responses.append(completion('done'))
    endpoint=EndpointConfig(url,'original-alias',supported_efforts=('low',),
        features={'streaming':False,'reasoning_effort_format':'thinking_toggle'})
    result=OpenAICompatibleBackend(endpoint).run_prompt(Invocation(str(tmp_path),
        'Return done.',{},10,{'prompt_metadata':{'model':model,'effort':'none'}}))
    assert result.exit_code==0,result.stderr
    if model==QWEN:
        assert requests[0]['chat_template_kwargs']=={'enable_thinking':False}
        assert 'reasoning_effort' not in requests[0]
    else:
        assert requests[0]['reasoning_effort']=='none'
        assert 'chat_template_kwargs' not in requests[0]


@pytest.mark.parametrize('wire_format,effort',[
    ('thinking_toggle','high'),('thinking_toggle','none'),
    ('chat_template_effort','medium'),('chat_template_effort','none')])
def test_conformance_keeps_native_effort_projection(server,wire_format,effort):
    from prosaic_runtime import RunPolicy
    from prosaic_runtime.diagnostics import conformance
    url,requests,responses=server
    endpoint=EndpointConfig(url,'custom-alias',features={'streaming':False,'json_mode':False,
        'reasoning_effort_format':wire_format,'reasoning_effort':effort})
    responses.append(completion('complete'))
    conformance(RuntimeConfig({'api':endpoint},{},'api'),'api',live=True,
        evidence_origin='fixture',policy=RunPolicy(timeout_s=10,max_provider_requests=1,
            max_tool_calls=1,max_reported_tokens=100))
    assert len(requests)==1
    assert requests[0]['chat_template_kwargs']=={'enable_thinking':effort!='none'}
    assert requests[0].get('reasoning_effort')==(
        effort if wire_format=='chat_template_effort' and effort!='none' else None)


def test_none_api_pool_skips_native_without_invoking_it(server, tmp_path):
    from prosaic_runtime import UnifiedRuntime
    from test_unified_runtime import runtime as unified_runtime, policy
    url, requests, responses = server
    base = unified_runtime(tmp_path, url)
    api = replace(base.config.api_config, profiles={'local': EndpointConfig(url,GEMMA,
        features={'streaming':False})})
    responses.append(completion('done'))
    result = UnifiedRuntime(replace(base.config,api_config=api)).run(
        artifact('none'),cwd=tmp_path,policy=policy())
    assert result.exit_code == 0, result.stderr
    assert requests[0]['chat_template_kwargs'] == {'enable_thinking':False}
    assert not (tmp_path/'claude.calls').exists()
    assert not (tmp_path/'codex.calls').exists()


@pytest.mark.parametrize('profile', ['qwen','gemma'])
@pytest.mark.parametrize('agent,enabled', [('thinking-solver',True),('direct-solver',False)])
def test_local_markdown_examples_inspect_and_execute(server, tmp_path, profile, agent, enabled):
    from pathlib import Path
    import subprocess
    import sys
    import yaml
    examples=Path(__file__).resolve().parents[1]/'examples'
    url, requests, responses=server
    data=yaml.safe_load((examples/'local-thinking.yml').read_text())
    for endpoint in data['profiles'].values():
        endpoint['base_url']=url
        endpoint.pop('api_key_env',None)
    config=tmp_path/'runtime.yml';config.write_text(yaml.safe_dump(data))
    responses.append(completion('done'))
    run=subprocess.run([sys.executable,'-m','prosaic_runtime.cli',f'subagents/{agent}.md',
        '--config',str(config),'--profile',profile,'--source',str(examples/'.prosaic'),
        '--arguments','Find n.'],capture_output=True,text=True,timeout=30,cwd=tmp_path)
    assert run.returncode == 0, run.stdout+run.stderr
    assert requests[0]['chat_template_kwargs'] == {'enable_thinking':enabled}
    assert requests[0].get('reasoning_effort') == ('low' if profile=='qwen' and enabled else None)
