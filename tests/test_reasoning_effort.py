"""Canonical effort reaches real HTTP/subprocess boundaries without paid calls."""
import json
import sys
import shutil
from dataclasses import replace

import pytest

from prosaic_runtime import (NativePolicy, NativeProfile, NativeRuntime,
                             NativeRuntimeConfig, ProfilePool, ProsaicArtifact,
                             ProsaicRuntime, EndpointConfig, RuntimeConfig)
from test_runtime import server, completion


def artifact(effort=None):
    return ProsaicArtifact.from_inspection({
        'id': 'commands/review.md', 'type': 'command', 'body': 'Review {{args}}.',
        'frontmatter': {} if effort is None else {'effort': effort}})


def native_fixture(tmp_path, backend, *, default=None):
    """The child returns the actual flags received at the native CLI boundary."""
    executable = tmp_path / backend
    executable.write_text(f'#!{sys.executable}\nimport json,sys\n'
        'sys.stdin.read()\n'
        'text = json.dumps(sys.argv[1:])\n'
        "if '--output-format' in sys.argv:\n"
        "    print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':text}))\n"
        'else:\n'
        "    print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':text}}))\n"
        "    print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}))\n")
    executable.chmod(0o755)
    return NativeRuntime(NativeRuntimeConfig({
        'native': NativeProfile(backend, 'example', 'account', str(executable), effort=default)},
        default_profile='native'))


def captured_effort(result, backend):
    assert result.exit_code == 0, result.stderr
    argv = json.loads(result.stdout)
    if backend == 'claude-code':
        return argv[argv.index('--effort') + 1] if '--effort' in argv else None
    settings = [v for v in argv if v.startswith('model_reasoning_effort=')]
    return json.loads(settings[0].split('=', 1)[1]) if settings else None


@pytest.mark.parametrize('backend', ['claude-code', 'codex-cli'])
@pytest.mark.parametrize('effort', ['low', 'medium', 'high'])
def test_artifact_effort_overrides_native_profile_default(tmp_path, backend, effort):
    runtime = native_fixture(tmp_path, backend, default='medium')
    result = runtime.run(artifact(effort), cwd=tmp_path, policy=NativePolicy())
    assert captured_effort(result, backend) == effort
    # An artifact override must not leak into the next assignment.
    result = runtime.run(artifact(), cwd=tmp_path, policy=NativePolicy())
    assert captured_effort(result, backend) == 'medium'
    assert runtime.config.profiles['native'].effort == 'medium'


@pytest.mark.parametrize('backend', ['claude-code', 'codex-cli'])
@pytest.mark.parametrize('default', [None, 'low', 'medium', 'high'])
def test_omitted_artifact_effort_uses_native_default(tmp_path, backend, default):
    runtime = native_fixture(tmp_path, backend, default=default)
    result = runtime.run(artifact(), cwd=tmp_path, policy=NativePolicy())
    assert captured_effort(result, backend) == default


@pytest.mark.parametrize('effort', ['', 'maximum', True, 1, ['low'], {'level': 'high'}])
def test_invalid_native_artifact_effort_fails_before_dispatch(tmp_path, effort):
    runtime = native_fixture(tmp_path, 'claude-code')
    events = []
    with pytest.raises(ValueError, match='effort'):
        runtime.run(artifact(effort), cwd=tmp_path, policy=NativePolicy(), on_event=events.append)
    assert events == []


@pytest.mark.parametrize('method', ['run', 'turn'])
@pytest.mark.parametrize('effort,default,want', [
    ('low', 'high', 'low'), ('medium', 'low', 'medium'), ('high', 'low', 'high'),
    ('high', None, 'high'), (None, 'medium', 'medium'), (None, None, None)])
def test_canonical_effort_reaches_openai_http_payload(server, tmp_path, method, effort, default, want):
    url, requests, responses = server
    runtime = ProsaicRuntime(RuntimeConfig({'api': EndpointConfig(url, 'example',
        features={'streaming': False, 'reasoning_effort': default})}, {}, 'api'))
    responses.append(completion('done'))
    if method == 'run':
        result = runtime.run(artifact(effort), cwd=tmp_path)
    else:
        result = runtime.turn(replace(artifact(effort), body='Review supplied input.'), message='Review').result
    assert result.exit_code == 0, result.stderr
    assert len(requests) == 1
    if want is None:
        assert 'reasoning_effort' not in requests[0]
    else:
        assert requests[0]['reasoning_effort'] == want


@pytest.mark.parametrize('effort,want', [('high', 'high'), (None, 'low')])
def test_effort_survives_native_pool_fallback(tmp_path, effort, want):
    from test_native_runtime import binary
    failed = binary(tmp_path, 'limited', [
        {'type': 'turn.failed', 'error': {'code': 'usage_limit_reached'}}], exit_code=1)
    fallback = native_fixture(tmp_path, 'claude-code', default='low').config.profiles['native']
    runtime = NativeRuntime(NativeRuntimeConfig({
        'first': NativeProfile('codex-cli', 'example', 'first', failed, effort='medium'),
        'second': replace(fallback, quota_key='second')},
        {'coding': ProfilePool(('first', 'second'))}, default_pool='coding'))
    result = runtime.run(artifact(effort), cwd=tmp_path, policy=NativePolicy())
    assert captured_effort(result, 'claude-code') == want
    assert [a['profile'] for a in result.metadata['native_execution_v1']['attempts']] == ['first', 'second']


@pytest.mark.parametrize('backend', ['claude-code', 'codex-cli'])
def test_native_effort_is_inspected_from_canonical_prose_without_target_copies(tmp_path, backend):
    if not shutil.which('prosaic'):
        pytest.skip('Prosaic CLI is required for inspection conformance')
    runtime = native_fixture(tmp_path, backend, default='low')
    runtime.source = tmp_path / '.prosaic'
    source = runtime.source / 'commands' / 'review.md'
    source.parent.mkdir(parents=True)
    source.write_text('---\ntype: command\neffort: high\n---\nReview {{args}}.\n')
    result = runtime.run('commands/review.md', cwd=tmp_path, policy=NativePolicy())
    assert captured_effort(result, backend) == 'high'
    assert not (tmp_path / '.claude').exists()
    assert not (tmp_path / '.codex').exists()
