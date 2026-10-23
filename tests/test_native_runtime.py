import json
import os
import sys
import time

import pytest

from prosaic_runtime import ProsaicArtifact


def artifact(**metadata):
    return ProsaicArtifact('subagents/test.md', 'subagent', metadata, 'Do {{args}}.')


def binary(tmp_path, name, events, *, exit_code=0):
    path = tmp_path / name
    trace = tmp_path / (name + '.calls')
    path.write_text(f'#!{sys.executable}\nimport sys\n'
        f'with open({str(trace)!r}, "a") as f: f.write(sys.stdin.read() + "\\n")\n'
        f'print({"".join(json.dumps(e) + chr(10) for e in events)!r}, end="")\n'
        f'sys.exit({exit_code})\n')
    path.chmod(0o755)
    return str(path)


def setup_runtime(tmp_path, *, effects=False, reason='usage_limit_reached', same_quota=False, capacity=None):
    from prosaic_runtime import NativeRuntime, NativeRuntimeConfig, NativeProfile, ProfilePool
    first = []
    if effects:
        first.append({'type': 'item.started', 'item': {'type': 'command_execution', 'command': 'edit'}})
    first.append({'type': 'turn.failed', 'error': {'code': reason}})
    fail = binary(tmp_path, 'primary', first, exit_code=1)
    good = binary(tmp_path, 'fallback', [
        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'done'}},
        {'type': 'turn.completed', 'usage': {'input_tokens': 4, 'output_tokens': 2}},
    ])
    profiles = {
        'primary': NativeProfile('codex-cli', 'test', 'one', fail),
        'fallback': NativeProfile('codex-cli', 'test', 'one' if same_quota else 'two', good),
    }
    config = NativeRuntimeConfig(profiles, {'coding': ProfilePool(('primary', 'fallback'))}, default_pool='coding')
    return NativeRuntime(config, capacity=capacity)


def test_pool_fallback_and_shared_state_across_invocations(tmp_path):
    from prosaic_runtime import NativePolicy
    runtime = setup_runtime(tmp_path)
    events = []
    result = runtime.run(artifact(), 'review', cwd=tmp_path, policy=NativePolicy(), on_event=events.append)
    assert result.exit_code == 0 and result.stdout == 'done'
    assert result.metadata['profile'] == 'fallback'
    attempts = result.metadata['native_execution_v1']['attempts']
    assert [a['profile'] for a in attempts] == ['primary', 'fallback']
    assert result.token_usage is None  # First attempt did not report usage.
    assert result.metadata['native_execution_v1']['known_tokens'] == 6
    assert any(e['event'] == 'profile_fallback' for e in events)
    runtime.run(artifact(), 'second', cwd=tmp_path, policy=NativePolicy())
    assert (tmp_path / 'primary.calls').read_text().count('Do') == 1


def test_shared_quota_alias_cannot_bypass_exhaustion(tmp_path):
    from prosaic_runtime import NativePolicy
    result = setup_runtime(tmp_path, same_quota=True).run(artifact(), cwd=tmp_path, policy=NativePolicy())
    assert result.metadata['failure_reason'] == 'capacity_unavailable'
    assert not (tmp_path / 'fallback.calls').exists()
    assert result.metadata['retry_at'] > time.time()


@pytest.mark.parametrize('effects,reason', [(True, 'usage_limit_reached'), (False, 'authentication_failed')])
def test_no_replay_after_effects_or_auth_failure(tmp_path, effects, reason):
    from prosaic_runtime import NativePolicy
    result = setup_runtime(tmp_path, effects=effects, reason=reason).run(
        artifact(), cwd=tmp_path, policy=NativePolicy())
    assert result.exit_code != 0
    assert not (tmp_path / 'fallback.calls').exists()
    if effects:
        assert result.metadata['recovery_required'] is True
        assert result.metadata['failure_reason'] == 'usage_limit'


def test_native_policy_is_explicit_and_not_an_api_policy(tmp_path):
    from prosaic_runtime import RunPolicy, NativePolicy
    runtime = setup_runtime(tmp_path)
    for policy in (None, RunPolicy(), NativePolicy(permission_mode='workspace-write')):
        with pytest.raises(ValueError, match='NativePolicy|permission'):
            runtime.run(artifact(), cwd=tmp_path, policy=policy)
    assert not (tmp_path / 'primary.calls').exists()
    assert 'read_receipts_v1' not in runtime.capabilities
    assert 'conversation_turn_v1' not in runtime.capabilities


def test_native_full_declaration_requires_explicit_write_grants(tmp_path):
    from dataclasses import replace
    from prosaic_runtime import NativeRuntime, NativePolicy
    base = setup_runtime(tmp_path)
    runtime = NativeRuntime(replace(base.config, profiles={
        name: replace(value, permission_mode='workspace-write')
        for name, value in base.config.profiles.items()}))
    with pytest.raises(ValueError, match='write permission'):
        runtime.run(artifact(tools='full'), cwd=tmp_path, policy=NativePolicy(), profile='fallback')
    result = runtime.run(artifact(tools='full'), cwd=tmp_path, profile='fallback',
                         policy=NativePolicy(permission_mode='workspace-write'))
    assert result.exit_code == 0


def test_explicit_profile_and_routes(tmp_path):
    from dataclasses import replace
    from prosaic_runtime import NativePolicy, NativeRuntime
    base = setup_runtime(tmp_path)
    runtime = NativeRuntime(replace(base.config, routes={'strong': 'fallback'}))
    assert runtime.run(artifact(model_tier='strong'), cwd=tmp_path, policy=NativePolicy()).exit_code == 0
    with pytest.raises(ValueError, match='route'):
        runtime.run(artifact(model_tier='missing'), cwd=tmp_path, policy=NativePolicy())
    assert runtime.run(artifact(), cwd=tmp_path, policy=NativePolicy(), profile='fallback').exit_code == 0
    with pytest.raises(ValueError, match='profile.*pool'):
        runtime.run(artifact(), cwd=tmp_path, policy=NativePolicy(), profile='primary', pool='coding')


def test_input_and_cancel_before_dispatch(tmp_path):
    from prosaic_runtime import NativePolicy
    runtime = setup_runtime(tmp_path)
    with pytest.raises(ValueError, match='max_input_bytes'):
        runtime.run(artifact(), 'x' * 100, cwd=tmp_path, policy=NativePolicy(max_input_bytes=10))
    result = runtime.run(artifact(), cwd=tmp_path, policy=NativePolicy(), cancelled=lambda: True)
    assert result.exit_code == 130
    assert not (tmp_path / 'primary.calls').exists()


def test_critical_callback_prevents_fallback(tmp_path):
    from prosaic_runtime import NativePolicy
    def sink(event):
        if event['event'] == 'profile_fallback':
            raise RuntimeError('callback failed')
    with pytest.raises(RuntimeError, match='callback failed'):
        setup_runtime(tmp_path).run(artifact(), cwd=tmp_path, policy=NativePolicy(), on_event=sink)
    assert not (tmp_path / 'fallback.calls').exists()


def test_cli_native_configuration(tmp_path, capsys):
    from prosaic_runtime.cli import main
    executable = binary(tmp_path, 'codex-fixture', [
        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'done'}},
        {'type': 'turn.completed'},
    ])
    config = tmp_path / 'native.yml'
    config.write_text(f'''kind: native
profiles:
  local:
    backend: codex-cli
    model: example
    quota_key: account
    executable: {executable}
default_profile: local
''')
    source = tmp_path / '.prosaic' / 'subagents'
    source.mkdir(parents=True)
    (source / 'review.md').write_text('---\nname: review\ndescription: Review\nexecution: agent\n---\nReview {{args}}.')
    assert main(['subagents/review.md', '--config', str(config), '--source', str(source.parent),
                 '--cwd', str(tmp_path), '--native-permissions', 'read-only', '--quiet']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['stdout'] == 'done' and result['metadata']['profile'] == 'local'


def test_fallback_shares_absolute_deadline(tmp_path):
    from prosaic_runtime import NativePolicy
    runtime = setup_runtime(tmp_path)
    for name in ('primary', 'fallback'):
        path = tmp_path / name
        delay = .1 if name == 'primary' else 2
        path.write_text(path.read_text().replace('import sys', f'import sys,time\ntime.sleep({delay})', 1))
    started = time.monotonic()
    result = runtime.run(artifact(), cwd=tmp_path, policy=NativePolicy(timeout_s=1))
    assert result.timed_out and result.metadata['failure_reason'] == 'timeout'
    assert time.monotonic() - started < 1.7
    assert [a['profile'] for a in result.metadata['native_execution_v1']['attempts']] == ['primary', 'fallback']


def test_pool_restores_primary_after_authoritative_refresh(tmp_path):
    from prosaic_runtime import NativePolicy
    runtime = setup_runtime(tmp_path)
    policy = NativePolicy()
    runtime.run(artifact(), cwd=tmp_path, policy=policy)
    runtime.capacity.mark_available('one')
    runtime.run(artifact(), cwd=tmp_path, policy=policy)
    assert (tmp_path / 'primary.calls').read_text().count('Do') == 2


def test_concurrent_tasks_keep_independent_results(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from prosaic_runtime import NativePolicy
    runtime = setup_runtime(tmp_path)
    # A host supplies separate workspaces; both use the same known-good profile.
    workspaces = [tmp_path / str(i) for i in range(4)]
    for workspace in workspaces:
        workspace.mkdir()
    def run(workspace):
        return runtime.run(artifact(), str(workspace), cwd=workspace,
                           profile='fallback', policy=NativePolicy())
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(run, workspaces))
    assert all(r.exit_code == 0 and r.token_usage == 6 for r in results)
    assert all(len(r.metadata['native_execution_v1']['attempts']) == 1 for r in results)


def test_cli_rejects_api_grants_for_native_config(tmp_path, capsys):
    from prosaic_runtime.cli import main
    path = tmp_path / 'native.yml'
    path.write_text('kind: native\n')
    assert main(['agent', '--config', str(path), '--native-permissions', 'read-only',
                 '--allow-tool', 'read_file']) == 2
    assert 'unsupported in native' in capsys.readouterr().err


def test_runtime_capacity_lock_respects_invocation_deadline(tmp_path):
    import fcntl
    from prosaic_runtime import NativePolicy, CapacityStore
    path = tmp_path / 'capacity.json'
    runtime = setup_runtime(tmp_path, capacity=CapacityStore(path))
    with open(str(path) + '.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        started = time.monotonic()
        result = runtime.run(artifact(), cwd=tmp_path, policy=NativePolicy(timeout_s=.05))
        assert result.timed_out and result.metadata['failure_reason'] == 'timeout'
        assert time.monotonic() - started < .5
    assert not (tmp_path / 'primary.calls').exists()


def test_existing_api_explicit_profile_override(server, tmp_path, capsys):
    # Reuse the real numeric-loopback HTTP fixture from test_runtime.
    from prosaic_runtime.cli import main
    from test_runtime import completion
    url, requests, responses = server
    responses.append(completion())
    config = tmp_path / 'api.yml'
    config.write_text(f'''profiles:
  offline:
    base_url: http://127.0.0.1:9/v1
    model: bad
  selected:
    base_url: {url}
    model: good
    features: {{streaming: false}}
default_profile: offline
routes: {{fast: offline}}
''')
    source = tmp_path / '.prosaic' / 'subagents'
    source.mkdir(parents=True)
    (source / 'test.md').write_text('---\nname: test\ndescription: Test\nexecution: agent\nmodel_tier: fast\n---\nAnswer.')
    assert main(['subagents/test.md', '--config', str(config), '--source', str(source.parent),
                 '--profile', 'selected', '--quiet']) == 0
    assert requests[0]['model'] == 'good'
    assert json.loads(capsys.readouterr().out)['metadata']['profile'] == 'selected'


from test_runtime import server  # Real transport fixture, no monkeypatched provider.
