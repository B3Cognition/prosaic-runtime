"""Documented provider examples exercise inspection and real HTTP boundaries."""
from dataclasses import replace
from pathlib import Path
import subprocess
import sys

import pytest

from prosaic_runtime import EndpointConfig, ProsaicRuntime, RuntimeConfig
from prosaic_runtime.effort import effective_efforts
from test_reasoning_effort import artifact
from test_runtime import server, completion


EXAMPLES = Path(__file__).resolve().parents[1] / 'examples'
MODELS = [
    ('deepseek', 'deepseek-v4-pro', ('low', 'high', 'max')),
    ('qwen', 'qwen3.8-max', ('low', 'medium', 'xhigh')),
    ('nemotron', 'nvidia/nemotron-3.5-super-vl-120b-a12b', ('low', 'medium', 'high')),
]


@pytest.mark.parametrize('profile,model,levels', MODELS)
def test_provider_model_capabilities_are_automatic(profile, model, levels):
    assert effective_efforts('openai-compatible', model) == levels


@pytest.mark.parametrize('profile,model,levels', MODELS)
def test_provider_examples_through_prosaic_cli_and_http(server, tmp_path, profile, model, levels):
    import yaml
    url, requests, responses = server
    config = yaml.safe_load((EXAMPLES / 'reasoning.yml').read_text())
    for endpoint in config['profiles'].values():
        endpoint['base_url'] = url
        endpoint.pop('api_key_env', None)
    path = tmp_path / 'runtime.yml'
    path.write_text(yaml.safe_dump(config))
    responses.append(completion('The answer is 6:25 pm.'))
    run = subprocess.run([
        sys.executable, '-m', 'prosaic_runtime.cli',
        f'subagents/{profile}-reasoner.md', '--config', str(path),
        '--source', str(EXAMPLES / '.prosaic'), '--profile', profile,
        '--arguments', 'What time is 3 hours and 40 minutes after 2:45 pm?',
    ], cwd=tmp_path, text=True, capture_output=True, timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr
    assert len(requests) == 1
    payload = requests[0]
    assert payload['model'] == model
    assert payload['reasoning_effort'] == levels[-1]
    assert 'temperature' not in payload
    assert 'tools' not in payload
    assert '3 hours and 40 minutes' in str(payload['messages'])
    assert '6:25 pm' in run.stdout


@pytest.mark.parametrize('profile,model,levels', MODELS)
def test_all_documented_levels_and_rejections(server, tmp_path, profile, model, levels):
    url, requests, responses = server
    runtime = ProsaicRuntime(RuntimeConfig({'api': EndpointConfig(
        url, model, temperature=None, features={'streaming': False})}, {}, 'api'))
    for effort in levels:
        responses.append(completion('done'))
        result = runtime.run(artifact(effort), cwd=tmp_path)
        assert result.exit_code == 0, result.stderr
        assert requests[-1]['reasoning_effort'] == effort
    for effort in set(('low', 'medium', 'high', 'xhigh', 'max', 'ultra')) - set(levels):
        with pytest.raises(ValueError, match='effort'):
            runtime.run(artifact(effort), cwd=tmp_path)
    assert len(requests) == len(levels)


@pytest.mark.parametrize('method', ['run', 'turn'])
def test_existing_api_none_control_is_preserved(server, tmp_path, method):
    url, requests, responses = server
    runtime = ProsaicRuntime(RuntimeConfig({'api': EndpointConfig(url, 'example',
        features={'streaming': False, 'reasoning_effort': ' NONE '})}, {}, 'api'))
    responses.append(completion('done'))
    prose = replace(artifact(), body='Answer the question.')
    result = runtime.run(prose, cwd=tmp_path) if method == 'run' else runtime.turn(prose, message='Question').result
    assert result.exit_code == 0, result.stderr
    assert requests[0]['reasoning_effort'] == 'none'


@pytest.mark.parametrize('effort,model,declared', [
    ('ultra', 'deepseek-v4-pro', None),
    ('medium', 'deepseek-v4-pro', None),
    ('max', 'deepseek-v4-pro', ('low',)),
    ('max', 'unknown-model', ('low', 'max')),
])
def test_constrained_requests_cannot_bypass_effort_admission(server, tmp_path, effort, model, declared):
    from prosaic_runtime.openai_compatible import OpenAICompatibleBackend
    from prosaic_runtime.types import Invocation
    url, requests, responses = server
    responses.append(completion('done'))
    backend = OpenAICompatibleBackend(EndpointConfig(url, 'deepseek-v4-pro',
        supported_efforts=declared, features={'reasoning_effort': effort}))
    result = backend.run_constrained_prompt(Invocation(str(tmp_path), 'Return done.', {}, 10),
        model=model, screen_output=lambda b: b, max_input_bytes=1024, max_capture_bytes=1024)
    assert result.exit_code != 0
    assert requests == []


def test_effort_capabilities_bind_qualification_identity():
    from prosaic_runtime.conformance import _profile_fingerprint
    endpoint = EndpointConfig('http://localhost/v1', 'deepseek-v4-pro')
    assert _profile_fingerprint(endpoint) != _profile_fingerprint(
        replace(endpoint, supported_efforts=('low',)))


@pytest.mark.parametrize('method', ['run', 'turn', 'constrained', 'unified'])
@pytest.mark.parametrize('effort', ['xhigh', 'max'])
def test_published_custom_api_defaults_remain_compatible(server, tmp_path, method, effort):
    from prosaic_runtime.openai_compatible import OpenAICompatibleBackend
    from prosaic_runtime.types import Invocation
    url, requests, responses = server
    endpoint = EndpointConfig(url, 'operator-custom-model',
        features={'streaming': False, 'reasoning_effort': effort})
    responses.append(completion('done'))
    if method == 'constrained':
        result = OpenAICompatibleBackend(endpoint).run_constrained_prompt(
            Invocation(str(tmp_path), 'Return done.', {}, 10),
            model=endpoint.model, screen_output=lambda b: b,
            max_input_bytes=1024, max_capture_bytes=1024)
    elif method == 'unified':
        from prosaic_runtime import UnifiedRuntime
        from test_unified_runtime import runtime as unified_runtime, policy
        base = unified_runtime(tmp_path, url, order=('local',))
        config = replace(base.config, api_config=replace(base.config.api_config,
            profiles={'local': endpoint}))
        result = UnifiedRuntime(config).run(artifact(), cwd=tmp_path, policy=policy())
    else:
        runtime = ProsaicRuntime(RuntimeConfig({'api': endpoint}, {}, 'api'))
        prose = replace(artifact(), body='Answer the question.')
        result = runtime.run(prose, cwd=tmp_path) if method == 'run' else runtime.turn(prose, message='Question').result
    assert result.exit_code == 0, result.stderr
    assert requests[0]['reasoning_effort'] == effort
