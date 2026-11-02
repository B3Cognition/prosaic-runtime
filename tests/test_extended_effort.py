"""Extended effort is explicit, model-bound, and never silently downgraded."""
from dataclasses import replace
import json

import pytest

from prosaic_runtime import (EndpointConfig, NativePolicy, NativeProfile, TurnAdmissionError,
    NativeRuntime, NativeRuntimeConfig, ProfilePool, ProsaicRuntime, RuntimeConfig,
    validate_execution_artifact)
from test_reasoning_effort import artifact, native_fixture, captured_effort
from test_runtime import server, completion
from test_prework_safety import rejection_server


@pytest.mark.parametrize('backend,effort,want', [
    ('claude-code', 'xhigh', 'xhigh'), ('claude-code', 'max', 'max'),
    ('codex-cli', 'xhigh', 'xhigh'),
    ('codex-cli', 'max', 'max'), ('codex-cli', 'ultra', 'ultra')])
@pytest.mark.parametrize('as_default', [False, True])
def test_extended_effort_reaches_native_command(tmp_path, backend, effort, want, as_default):
    base = native_fixture(tmp_path, backend)
    profile = replace(base.config.profiles['native'], supported_efforts=('low', effort),
                      effort=effort if as_default else 'low')
    runtime = NativeRuntime(replace(base.config, profiles={'native': profile}))
    result = runtime.run(artifact(None if as_default else effort), cwd=tmp_path, policy=NativePolicy())
    assert captured_effort(result, backend) == want


@pytest.mark.parametrize('effort', ['xhigh', 'max'])
@pytest.mark.parametrize('permission_mode', ['read-only', 'workspace-write'])
def test_claude_effort_preserves_existing_permission_boundaries(tmp_path, effort, permission_mode):
    base = native_fixture(tmp_path, 'claude-code')
    profile = replace(base.config.profiles['native'], supported_efforts=(effort,),
                      permission_mode=permission_mode)
    runtime = NativeRuntime(replace(base.config, profiles={'native': profile}))
    result = runtime.run(artifact(effort), cwd=tmp_path,
                         policy=NativePolicy(permission_mode=permission_mode))
    assert result.exit_code == 0, result.stderr
    argv = json.loads(result.stdout)
    want = {'Read', 'Glob', 'Grep'}
    if permission_mode == 'workspace-write':
        want |= {'Edit', 'Write', 'Bash'}
    for flag in ('--tools', '--allowedTools'):
        assert set(argv[argv.index(flag) + 1].split(',')) == want
    assert argv[argv.index('--permission-mode') + 1] == 'dontAsk'
    assert '--restricted' in argv and '--strict-mcp-config' in argv


@pytest.mark.parametrize('method', ['run', 'turn'])
@pytest.mark.parametrize('effort', ['xhigh', 'max'])
@pytest.mark.parametrize('as_default', [False, True])
def test_extended_effort_reaches_openai_payload(server, tmp_path, method, effort, as_default):
    url, requests, responses = server
    endpoint = EndpointConfig(url, 'example', supported_efforts=('low', effort),
        features={'streaming': False, 'reasoning_effort': effort if as_default else 'low'})
    runtime = ProsaicRuntime(RuntimeConfig({'api': endpoint}, {}, 'api'))
    value = replace(artifact(None if as_default else effort), body='Review the input.')
    responses.append(completion('done'))
    result = runtime.run(value, cwd=tmp_path) if method == 'run' else runtime.turn(value, message='Review').result
    assert result.exit_code == 0, result.stderr
    assert requests[0]['reasoning_effort'] == effort


@pytest.mark.parametrize('effort', ['xhigh', 'max', 'ultra'])
def test_unknown_model_capabilities_reject_extended_effort(tmp_path, effort):
    runtime = native_fixture(tmp_path, 'codex-cli')
    with pytest.raises(ValueError, match='effort'):
        runtime.run(artifact(effort), cwd=tmp_path, policy=NativePolicy())
    config = RuntimeConfig({'api': EndpointConfig('http://localhost:9/v1', 'unknown')}, {}, 'api')
    with pytest.raises(ValueError, match='effort'):
        validate_execution_artifact(artifact(effort), config)


def test_ultra_is_not_declared_on_providers_without_native_support():
    with pytest.raises(ValueError, match='effort'):
        EndpointConfig('http://localhost/v1', 'example', supported_efforts=('ultra',))
    with pytest.raises(ValueError, match='effort'):
        NativeProfile('claude-code', 'sonnet', 'account', supported_efforts=('ultra',))


@pytest.mark.parametrize('supported', ['max', [1], ['ultracode'], ['max', 'max'], {'max': True}])
@pytest.mark.parametrize('native', [False, True])
def test_invalid_model_effort_declarations_are_rejected(supported, native):
    with pytest.raises(ValueError, match='effort'):
        if native:
            NativeProfile('codex-cli', 'example', 'account', supported_efforts=supported)
        else:
            EndpointConfig('http://localhost/v1', 'example', supported_efforts=supported)


def test_selected_model_route_controls_admission():
    profiles = {
        'limited': EndpointConfig('http://localhost/v1', 'limited', supported_efforts=('low', 'high')),
        'deep': EndpointConfig('http://localhost/v1', 'deep', supported_efforts=('low', 'xhigh', 'max'))}
    config = RuntimeConfig(profiles, {'strong': 'deep'}, 'limited')
    with pytest.raises(ValueError, match='effort'):
        validate_execution_artifact(artifact('max'), config)
    value = artifact('max')
    value.frontmatter['model_tier'] = 'strong'
    assert validate_execution_artifact(value, config) is None


def test_native_pool_skips_incompatible_model_before_dispatch(tmp_path):
    limited_dir, deep_dir = tmp_path / 'limited', tmp_path / 'deep'
    limited_dir.mkdir()
    deep_dir.mkdir()
    limited = native_fixture(limited_dir, 'claude-code').config.profiles['native']
    deep = replace(native_fixture(deep_dir, 'codex-cli').config.profiles['native'],
                   quota_key='deep', supported_efforts=('ultra',))
    runtime = NativeRuntime(NativeRuntimeConfig({'limited': limited, 'deep': deep},
        {'coding': ProfilePool(('limited', 'deep'))}, default_pool='coding'))
    events = []
    result = runtime.run(artifact('ultra'), cwd=tmp_path, policy=NativePolicy(), on_event=events.append)
    assert captured_effort(result, 'codex-cli') == 'ultra'
    assert [a['profile'] for a in result.metadata['native_execution_v1']['attempts']] == ['deep']
    assert any(e.get('profile') == 'limited' and e.get('reason') == 'effort_unavailable' for e in events)


@pytest.mark.parametrize('native', [False, True])
def test_yaml_binds_supported_efforts_to_the_profile_model(tmp_path, native):
    path = tmp_path / 'runtime.yml'
    if native:
        path.write_text('kind: native\ndefault_profile: deep\nprofiles:\n  deep:\n'
            '    backend: codex-cli\n    model: example\n    quota_key: deep\n'
            '    effort: ultra\n    supported_efforts: [low, xhigh, max, ultra]\n')
        config = NativeRuntimeConfig.load(path)
        assert config.profiles['deep'].effort == 'ultra'
    else:
        path.write_text('default_profile: deep\nprofiles:\n  deep:\n'
            '    base_url: http://localhost/v1\n    model: example\n'
            '    supported_efforts: [low, xhigh, max]\n')
        config = RuntimeConfig.load(path)
    assert config.profiles['deep'].supported_efforts[:3] == ('low', 'xhigh', 'max')


def test_no_effort_models_accept_omission_and_reject_any_explicit_level():
    endpoint = EndpointConfig('http://localhost/v1', 'plain', supported_efforts=[])
    config = RuntimeConfig({'plain': endpoint}, {}, 'plain')
    assert endpoint.supported_efforts == ()
    assert validate_execution_artifact(artifact(), config) is None
    with pytest.raises(ValueError, match='effort'):
        validate_execution_artifact(artifact('low'), config)


def test_model_override_cannot_reuse_another_models_effort_capabilities(tmp_path):
    from prosaic_runtime.openai_compatible import OpenAICompatibleBackend
    from prosaic_runtime.types import Invocation
    backend = OpenAICompatibleBackend(EndpointConfig('http://localhost:9/v1', 'deep',
        supported_efforts=('max',), features={'streaming': False}))
    with pytest.raises(ValueError, match='effort'):
        backend.run_prompt(Invocation(cwd=str(tmp_path), prompt='Review', env={}, timeout_s=1,
            metadata={'prompt_metadata': {'model': 'other', 'effort': 'max'}}))


def test_effort_capabilities_are_part_of_prepared_conversation_binding():
    endpoint = EndpointConfig('http://localhost:9/v1', 'deep', supported_efforts=('low', 'max'),
                              features={'streaming': False})
    runtime = ProsaicRuntime(RuntimeConfig({'deep': endpoint}, {}, 'deep'))
    prepared = runtime.prepare_turn(replace(artifact('max'), body='Review.'), message='Review')
    runtime.config.profiles['deep'] = replace(endpoint, supported_efforts=('low', 'high', 'max'))
    with pytest.raises(TurnAdmissionError) as exc:
        runtime.turn(prepared)
    assert exc.value.code == 'bindingMismatch'


def test_unified_pool_skips_unsupported_native_models_before_using_api(rejection_server, tmp_path):
    from test_unified_runtime import runtime as unified_runtime, policy
    url, requests, responses = rejection_server
    base = unified_runtime(tmp_path, url)
    base = type(base)(replace(base.config, native_config=replace(base.config.native_config,
        profiles={name: replace(profile, supported_efforts=('low', 'medium', 'high'))
                  for name, profile in base.config.native_config.profiles.items()})))
    api = base.config.api_config
    endpoint = replace(api.profiles['local'], supported_efforts=('low', 'max'))
    import prosaic_runtime as sdk
    runtime = sdk.UnifiedRuntime(replace(base.config, api_config=replace(api,
        profiles={'local': endpoint})))
    events = []
    responses.append((200, completion('done')[1], 0))
    result = runtime.run(artifact('max'), cwd=tmp_path, policy=policy(), on_event=events.append)
    assert result.exit_code == 0, result.stderr
    assert requests[0]['reasoning_effort'] == 'max'
    assert not (tmp_path / 'claude.calls').exists()
    assert not (tmp_path / 'codex.calls').exists()
    assert [e['profile'] for e in events if e.get('reason') == 'effort_unavailable'] == ['claude', 'codex']


@pytest.mark.parametrize('default,want', [(' HIGH ', 'high'), ('off', None), (False, None), (17, None)])
@pytest.mark.parametrize('method', ['run', 'turn'])
def test_api_profile_defaults_keep_existing_normalization(server, tmp_path, default, want, method):
    url, requests, responses = server
    endpoint = EndpointConfig(url, 'example', features={'streaming': False, 'reasoning_effort': default})
    runtime = ProsaicRuntime(RuntimeConfig({'api': endpoint}, {}, 'api'))
    responses.append(completion('done'))
    value = replace(artifact(), body='Review.')
    result = runtime.run(value, cwd=tmp_path) if method == 'run' else runtime.turn(value, message='Review').result
    assert result.exit_code == 0, result.stderr
    assert requests[0].get('reasoning_effort') == want
