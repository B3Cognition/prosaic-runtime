"""Automatic effort support uses model metadata, never a model turn."""
from dataclasses import replace
import json
import os
import sys
import time

import pytest

from prosaic_runtime import (EndpointConfig, NativePolicy, NativeProfile, NativeRuntime,
                             NativeRuntimeConfig, ProsaicRuntime, RuntimeConfig,
                             validate_execution_artifact)
from test_reasoning_effort import artifact, captured_effort, native_fixture
from test_runtime import server, completion


@pytest.mark.parametrize('provider,model,effort', [
    ('openai-compatible', 'gpt-6.1-sol', 'max'),
    ('openai-compatible', 'gpt-6-luna', 'xhigh'),
    ('claude-code', 'claude-opus-4-7', 'max'),
    ('claude-code', 'claude-sonnet-5-5', 'xhigh'),
    ('codex-cli', 'gpt-6-astra', 'ultra')])
def test_known_models_need_no_per_profile_effort_declaration(tmp_path, provider, model, effort):
    if provider == 'openai-compatible':
        config = RuntimeConfig({'api': EndpointConfig('http://localhost/v1', model)}, {}, 'api')
        assert validate_execution_artifact(artifact(effort), config) is None
    else:
        base = native_fixture(tmp_path, provider)
        profile = replace(base.config.profiles['native'], model=model)
        result = NativeRuntime(replace(base.config, profiles={'native': profile})).run(
            artifact(effort), cwd=tmp_path, policy=NativePolicy())
        assert captured_effort(result, provider) == effort


@pytest.mark.parametrize('model,effort', [('gpt-6-luna', 'ultra'), ('claude-opus-4-6', 'xhigh')])
def test_known_model_limits_are_not_inferred_from_provider_maximum(tmp_path, model, effort):
    backend = 'claude-code' if model.startswith('claude') else 'codex-cli'
    base = native_fixture(tmp_path, backend)
    profile = replace(base.config.profiles['native'], model=model)
    with pytest.raises(ValueError, match='effort'):
        NativeRuntime(replace(base.config, profiles={'native': profile})).run(
            artifact(effort), cwd=tmp_path, policy=NativePolicy())


def metadata_fixture(tmp_path, backend, levels, *, model='future-model', mode='ok'):
    path, trace = tmp_path / backend, tmp_path / 'metadata.calls'
    if backend == 'codex-cli':
        row = {'id': model, 'model': model, 'supportedReasoningEfforts': [
            {'reasoningEffort': value, 'description': value} for value in levels]}
    else:
        row = {'value': model, 'resolvedModel': model, 'supportsEffort': True,
               'supportedEffortLevels': levels}
    path.write_text(f'#!{sys.executable}\nimport json,sys,time\n'
        f'row={row!r}\nmode={mode!r}\n'
        "if 'app-server' in sys.argv or '--input-format' in sys.argv:\n"
        f'    with open({str(trace)!r},"a") as f: f.write("probe\\n")\n'
        '    for line in sys.stdin:\n'
        '        req=json.loads(line)\n'
        "        assert req.get('method') not in ('thread/start','turn/start')\n"
        "        assert req.get('type') != 'user'\n"
        "        if mode=='hang': time.sleep(10)\n"
        "        if mode=='malformed': print('{bad',flush=True); continue\n"
        "        if req.get('method')=='initialize':\n"
        "            print(json.dumps({'id':req['id'],'result':{'userAgent':'fixture'}}),flush=True)\n"
        "        elif req.get('method')=='model/list':\n"
        "            result={'data':[row],'nextCursor':None}\n"
        "            if mode=='paged' and 'cursor' not in req['params']: result={'data':[],'nextCursor':'page2'}\n"
        "            print(json.dumps({'id':req['id'],'result':result}),flush=True)\n"
        "        elif req.get('type')=='control_request':\n"
        "            assert req['request']['subtype']=='initialize'\n"
        "            print(json.dumps({'type':'control_response','response':{'subtype':'success',"
        "'request_id':req['request_id'],'response':{'models':[row]}}}),flush=True)\n"
        'else:\n'
        f'    with open({str(tmp_path / "inference.calls")!r},"a") as f: f.write("run\\n")\n'
        '    sys.stdin.read()\n    text=json.dumps(sys.argv[1:])\n'
        "    if '--output-format' in sys.argv:\n"
        "        print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':text}))\n"
        '    else:\n'
        "        print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':text}}))\n"
        "        print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}))\n")
    path.chmod(0o755)
    return NativeProfile(backend, model, 'account', str(path)), trace


@pytest.mark.parametrize('backend,effort', [('claude-code', 'max'), ('codex-cli', 'ultra')])
@pytest.mark.parametrize('as_default', [False, True])
def test_native_discovery_admits_advertised_levels_without_inference(tmp_path, backend, effort, as_default):
    profile, trace = metadata_fixture(tmp_path, backend, ['low', effort])
    if as_default:
        profile = replace(profile, effort=effort)
    runtime = NativeRuntime(NativeRuntimeConfig({'native': profile}, default_profile='native'))
    for unused in range(2):
        result = runtime.run(artifact(None if as_default else effort), cwd=tmp_path, policy=NativePolicy())
        assert captured_effort(result, backend) == effort
    assert trace.read_text().splitlines() == ['probe']  # Metadata is cached, not inferred twice.


@pytest.mark.parametrize('backend', ['claude-code', 'codex-cli'])
def test_advertised_limits_override_the_documented_fallback(tmp_path, backend):
    model = 'sonnet' if backend == 'claude-code' else 'gpt-6.1-sol'
    profile, trace = metadata_fixture(tmp_path, backend, ['low', 'high'], model=model)
    runtime = NativeRuntime(NativeRuntimeConfig({'native': profile}, default_profile='native'))
    with pytest.raises(ValueError, match='effort'):
        runtime.run(artifact('max'), cwd=tmp_path, policy=NativePolicy())
    assert trace.exists()
    assert not (tmp_path / 'inference.calls').exists()


@pytest.mark.parametrize('mode', ['malformed', 'hang'])
def test_discovery_failure_is_bounded_and_does_not_guess_unknown_models(tmp_path, mode):
    profile, trace = metadata_fixture(tmp_path, 'codex-cli', ['ultra'], mode=mode)
    runtime = NativeRuntime(NativeRuntimeConfig({'native': profile}, default_profile='native'))
    result = None
    started = time.monotonic()
    try:
        # Include interpreter startup on loaded native runners. The hanging
        # fixture sleeps for ten seconds, so this still exercises cancellation.
        result = runtime.run(artifact('ultra'), cwd=tmp_path, policy=NativePolicy(timeout_s=2))
    except ValueError as exc:
        assert 'effort' in str(exc)
    if result is not None:
        assert result.exit_code != 0
    assert time.monotonic() - started < 4
    assert trace.exists()
    assert not (tmp_path / 'inference.calls').exists()


def test_api_known_model_effort_reaches_wire_without_capability_configuration(server, tmp_path):
    url, requests, responses = server
    runtime = ProsaicRuntime(RuntimeConfig({'api': EndpointConfig(url, 'gpt-6.1-sol',
        features={'streaming': False})}, {}, 'api'))
    responses.append(completion('done'))
    assert runtime.run(artifact('max'), cwd=tmp_path).exit_code == 0
    assert requests[0]['reasoning_effort'] == 'max'


def test_unified_native_selection_uses_automatic_metadata(tmp_path):
    from prosaic_runtime import UnifiedRuntime, UnifiedRuntimeConfig
    from test_unified_runtime import policy
    from prosaic_runtime import ProfilePool
    profile, trace = metadata_fixture(tmp_path, 'codex-cli', ['high', 'ultra'])
    native = NativeRuntimeConfig({'future': profile}, default_profile='future')
    runtime = UnifiedRuntime(UnifiedRuntimeConfig(native, None,
        {'coding': ProfilePool(('future',))}, 'coding', {}))
    result = runtime.run(artifact('ultra'), cwd=tmp_path, policy=policy())
    assert captured_effort(result, 'codex-cli') == 'ultra'
    assert result.metadata['unified_execution_v1']['native_attempts'] == 1
    assert trace.read_text().splitlines() == ['probe']


def test_codex_metadata_follows_model_list_pagination(tmp_path):
    profile, trace = metadata_fixture(tmp_path, 'codex-cli', ['ultra'], mode='paged')
    runtime = NativeRuntime(NativeRuntimeConfig({'native': profile}, default_profile='native'))
    result = runtime.run(artifact('ultra'), cwd=tmp_path, policy=NativePolicy())
    assert captured_effort(result, 'codex-cli') == 'ultra'


def test_explicit_override_avoids_unnecessary_native_discovery(tmp_path):
    profile, trace = metadata_fixture(tmp_path, 'codex-cli', ['low'], mode='hang')
    profile = replace(profile, supported_efforts=('max',))
    runtime = NativeRuntime(NativeRuntimeConfig({'native': profile}, default_profile='native'))
    result = runtime.run(artifact('max'), cwd=tmp_path, policy=NativePolicy())
    assert captured_effort(result, 'codex-cli') == 'max'
    assert not trace.exists()


def test_native_metadata_cache_is_scoped_to_execution_environment(tmp_path):
    profile, trace = metadata_fixture(tmp_path, 'codex-cli', ['ultra'])
    runtime = NativeRuntime(NativeRuntimeConfig({'native': profile}, default_profile='native'))
    for context in ('one', 'two'):
        result = runtime.run(artifact('ultra'), cwd=tmp_path, policy=NativePolicy(),
                             env={**os.environ, 'MODEL_METADATA_CONTEXT': context})
        assert result.exit_code == 0
    assert trace.read_text().splitlines() == ['probe', 'probe']


def test_automatic_api_admission_and_conversation_preparation_remain_pure(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('pure admission attempted external discovery')
    monkeypatch.setattr('subprocess.Popen', forbidden)
    monkeypatch.setattr('urllib.request.build_opener', forbidden)
    endpoint = EndpointConfig('http://localhost:9/v1', 'gpt-6.1-sol')
    config = RuntimeConfig({'api': endpoint}, {}, 'api')
    assert validate_execution_artifact(artifact('max'), config) is None
    runtime = ProsaicRuntime(config)
    assert runtime.prepare_turn(replace(artifact('max'), body='Review.'), message='Review').input_bytes > 0


def test_failed_metadata_probe_does_not_poison_a_later_retry(tmp_path):
    profile, trace = metadata_fixture(tmp_path, 'codex-cli', ['max'], mode='hang')
    runtime = NativeRuntime(NativeRuntimeConfig({'native': profile}, default_profile='native'))
    assert runtime.run(artifact('max'), cwd=tmp_path, policy=NativePolicy(timeout_s=.4)).exit_code != 0
    metadata_fixture(tmp_path, 'codex-cli', ['max'])
    result = runtime.run(artifact('max'), cwd=tmp_path, policy=NativePolicy(timeout_s=2))
    assert captured_effort(result, 'codex-cli') == 'max'


def test_unified_delegate_reuses_resolved_fallback_without_a_second_probe(tmp_path):
    from prosaic_runtime import UnifiedRuntime, UnifiedRuntimeConfig, ProfilePool
    from test_unified_runtime import policy
    profile, trace = metadata_fixture(tmp_path, 'codex-cli', ['max'], model='gpt-6.1-sol', mode='malformed')
    native = NativeRuntimeConfig({'known': profile}, default_profile='known')
    runtime = UnifiedRuntime(UnifiedRuntimeConfig(native, None,
        {'coding': ProfilePool(('known',))}, 'coding', {}))
    result = runtime.run(artifact('max'), cwd=tmp_path, policy=policy())
    assert captured_effort(result, 'codex-cli') == 'max'
    assert trace.read_text().splitlines() == ['probe']


def test_native_pool_does_not_probe_unused_fallback_profiles(tmp_path):
    from prosaic_runtime import ProfilePool
    first_dir, later_dir = tmp_path / 'first', tmp_path / 'later'
    first_dir.mkdir()
    later_dir.mkdir()
    first, first_trace = metadata_fixture(first_dir, 'codex-cli', ['ultra'], model='first-model')
    later, later_trace = metadata_fixture(later_dir, 'codex-cli', ['ultra'], model='later-model', mode='hang')
    runtime = NativeRuntime(NativeRuntimeConfig({'first': first, 'later': later},
        {'coding': ProfilePool(('first', 'later'))}, default_pool='coding'))
    result = runtime.run(artifact('ultra'), cwd=tmp_path, policy=NativePolicy(timeout_s=1))
    assert captured_effort(result, 'codex-cli') == 'ultra'
    assert first_trace.exists()
    assert not later_trace.exists()


@pytest.mark.parametrize('unified', [False, True])
def test_cooling_down_profiles_are_skipped_before_metadata_probes(tmp_path, unified):
    from prosaic_runtime import CapacityStore, ProfilePool, UnifiedRuntime, UnifiedRuntimeConfig
    from test_unified_runtime import policy
    blocked_dir, ready_dir = tmp_path / 'blocked', tmp_path / 'ready'
    blocked_dir.mkdir()
    ready_dir.mkdir()
    blocked, blocked_trace = metadata_fixture(blocked_dir, 'codex-cli', ['ultra'], mode='hang')
    ready, unused = metadata_fixture(ready_dir, 'codex-cli', ['ultra'])
    blocked = replace(blocked, quota_key='blocked')
    ready = replace(ready, quota_key='ready', supported_efforts=('ultra',))
    config = NativeRuntimeConfig({'blocked': blocked, 'ready': ready},
        {'coding': ProfilePool(('blocked', 'ready'))}, default_pool='coding')
    capacity = CapacityStore()
    capacity.mark_unavailable('blocked', reason='usage_limit', until=time.time() + 60)
    if unified:
        runtime = UnifiedRuntime(UnifiedRuntimeConfig(config, None, config.pools, 'coding', {}), capacity=capacity)
        result = runtime.run(artifact('ultra'), cwd=tmp_path, policy=policy(timeout_s=1))
    else:
        runtime = NativeRuntime(config, capacity=capacity)
        result = runtime.run(artifact('ultra'), cwd=tmp_path, policy=NativePolicy(timeout_s=1))
    assert captured_effort(result, 'codex-cli') == 'ultra'
    assert not blocked_trace.exists()


@pytest.mark.parametrize('error_type', [ValueError, OSError])
def test_metadata_discovery_does_not_swallow_host_callback_failures(tmp_path, error_type):
    profile, trace = metadata_fixture(tmp_path, 'codex-cli', ['max'], model='gpt-6.1-sol')
    runtime = NativeRuntime(NativeRuntimeConfig({'native': profile}, default_profile='native'))
    raised = False
    def cancelled():
        nonlocal raised
        if trace.exists() and not raised:
            raised = True
            raise error_type('host cancelled callback failed')
        return False
    with pytest.raises(error_type, match='host cancelled callback failed'):
        runtime.run(artifact('max'), cwd=tmp_path, policy=NativePolicy(), cancelled=cancelled)
    assert not (tmp_path / 'inference.calls').exists()


@pytest.mark.parametrize('unified', [False, True])
def test_one_shot_metadata_cancellation_never_falls_through_to_inference(tmp_path, unified):
    from prosaic_runtime import ProfilePool, UnifiedRuntime, UnifiedRuntimeConfig
    from test_unified_runtime import policy
    profile, trace = metadata_fixture(tmp_path, 'codex-cli', ['max'], model='gpt-6.1-sol')
    config = NativeRuntimeConfig({'native': profile}, default_profile='native')
    consumed = False
    def cancelled():
        nonlocal consumed
        if trace.exists() and not consumed:
            consumed = True
            return True
        return False
    if unified:
        runtime = UnifiedRuntime(UnifiedRuntimeConfig(config, None,
            {'coding': ProfilePool(('native',))}, 'coding', {}))
        result = runtime.run(artifact('max'), cwd=tmp_path, policy=policy(), cancelled=cancelled)
    else:
        runtime = NativeRuntime(config)
        result = runtime.run(artifact('max'), cwd=tmp_path, policy=NativePolicy(), cancelled=cancelled)
    assert result.exit_code == 130
    assert not (tmp_path / 'inference.calls').exists()
