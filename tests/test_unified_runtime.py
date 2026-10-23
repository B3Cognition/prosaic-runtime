from dataclasses import replace
import json
import time

import pytest
import prosaic_runtime as sdk
from test_native_runtime import binary
from test_prework_safety import api, artifact, rejection_server
from test_runtime import completion


def policy(**values):
    assert hasattr(sdk, 'PoolPolicy'), 'missing unified policy'
    return sdk.PoolPolicy(**(dict(timeout_s=5, max_attempts=3, max_native_attempts=2,
        native_policy=sdk.NativePolicy(),
        api_policy=sdk.RunPolicy(max_provider_requests=3, max_tool_calls=2)) | values))


def runtime(tmp_path, url, *, order=('claude', 'codex', 'local'), native_error='usage_limit_reached',
            effects=False, quota=None, same_quota=False):
    assert hasattr(sdk, 'UnifiedRuntime'), 'missing unified Runtime'
    claude = binary(tmp_path, 'claude', [
        {'type': 'result', 'subtype': 'error_during_execution', 'is_error': True,
         'num_turns': 0, 'error': native_error}], exit_code=1)
    events = ([{'type': 'item.started', 'item': {'type': 'command_execution'}}] if effects else [])
    if native_error == 'success':
        events += [{'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'native-done'}},
                   {'type': 'turn.completed', 'usage': {'input_tokens': 4, 'output_tokens': 2}}]
    else:
        events += [{'type': 'turn.failed', 'error': {'code': native_error}}]
    codex = binary(tmp_path, 'codex', events, exit_code=0 if native_error == 'success' else 1)
    native = sdk.NativeRuntimeConfig({'claude': sdk.NativeProfile('claude-code', 'test', 'c', claude),
        'codex': sdk.NativeProfile('codex-cli', 'test', 'c' if same_quota else 'x', codex)},
        default_profile='claude')
    local = api(url).config
    c = sdk.UnifiedRuntimeConfig(native, local, {'agents': sdk.ProfilePool(order, cooldown_s=10)},
        'agents', {'local': 'c' if same_quota else 'l'}, capacity_file=str(tmp_path / 'capacity.json'))
    return sdk.UnifiedRuntime(c, capacity=quota)


def test_three_backend_prework_failover_keeps_unknown_usage_and_distinct_counts(rejection_server, tmp_path):
    url, requests, responses = rejection_server
    responses.append((200, completion('local-done')[1], 0))
    r = runtime(tmp_path, url)
    result = r.run(artifact(), cwd=tmp_path, policy=policy())
    assert result.exit_code == 0 and result.stdout == 'local-done'
    evidence = result.metadata['unified_execution_v1']
    assert [a['profile'] for a in evidence['attempts']] == ['claude', 'codex', 'local']
    assert evidence['native_attempts'] == 2 and evidence['api_provider_requests'] == 1
    assert evidence['api_tool_calls'] == 0 and evidence['known_tokens'] == 7
    assert result.token_usage is None and result.metadata['token_usage_status'] == 'unknown'
    assert len(requests) == 1
    assert r.capacity.status('c')['until'] < time.time() + 12
    # A new facade sees the same persisted native cooldowns.
    responses.append((200, completion('again')[1], 0))
    again = sdk.UnifiedRuntime(r.config).run(artifact(), cwd=tmp_path, policy=policy())
    assert again.metadata['unified_execution_v1']['native_attempts'] == 0
    assert (tmp_path / 'claude.calls').read_text().count('Answer') == 1


def test_api_first_429_falls_back_to_real_native_transport(rejection_server, tmp_path):
    url, _, responses = rejection_server
    responses.append((429, b'capacity', 0))
    result = runtime(tmp_path, url, order=('local', 'codex'), native_error='success').run(
        artifact(), cwd=tmp_path, policy=policy())
    assert result.exit_code == 0 and result.stdout == 'native-done'
    assert [a['profile'] for a in result.metadata['unified_execution_v1']['attempts']] == ['local', 'codex']
    assert result.metadata['unified_execution_v1']['api_provider_requests'] == 1


@pytest.mark.parametrize('status,body,delay', [(401, b'unauthorized', 0), (503, b'bad', 0),
    (200, b'malformed', 0), (200, b'late', .2)])
def test_api_ambiguous_failure_never_dispatches_native(rejection_server, tmp_path, status, body, delay):
    url, requests, responses = rejection_server
    responses.append((status, body, delay))
    result = runtime(tmp_path, url, order=('local', 'codex'), native_error='success').run(
        artifact(), cwd=tmp_path, policy=policy(api_policy=sdk.RunPolicy(timeout_s=.08,
            max_provider_requests=2, max_tool_calls=0)))
    assert result.exit_code != 0 and len(requests) == 1
    assert not (tmp_path / 'codex.calls').exists()
    assert result.metadata['recovery_required']


def test_api_tool_then_429_stops_without_replay(rejection_server, tmp_path):
    url, _, responses = rejection_server
    call = {'id': 'one', 'type': 'function', 'function': {'name': 'write_file',
        'arguments': '{"path":"out","content":"effect"}'}}
    responses.extend([(200, completion('', [call])[1], 0), (429, b'limited', 0)])
    r = runtime(tmp_path, url, order=('local', 'codex'), native_error='success')
    r = sdk.UnifiedRuntime(replace(r.config, api_config=replace(r.config.api_config,
        allowed_tools=frozenset({'write_file'}))))
    result = r.run(artifact(['write_file']), cwd=tmp_path, policy=policy(
        api_policy=sdk.RunPolicy(allowed_tools=frozenset({'write_file'}), write_paths=('out',),
                                max_provider_requests=3, max_tool_calls=1)))
    assert (tmp_path / 'out').read_text() == 'effect'
    assert result.exit_code != 0 and result.metadata['recovery_required']
    assert not (tmp_path / 'codex.calls').exists()


@pytest.mark.parametrize('effects,reason', [(True, 'usage_limit_reached'), (False, 'authentication_failed')])
def test_native_effect_or_auth_failure_never_dispatches_api(rejection_server, tmp_path, effects, reason):
    url, requests, _ = rejection_server
    result = runtime(tmp_path, url, order=('codex', 'local'), effects=effects, native_error=reason).run(
        artifact(), cwd=tmp_path, policy=policy())
    assert result.exit_code != 0 and requests == []
    assert len(result.metadata['unified_execution_v1']['attempts']) == 1


def test_shared_quota_and_all_unavailable_do_not_bypass_account(rejection_server, tmp_path):
    url, requests, _ = rejection_server
    r = runtime(tmp_path, url, same_quota=True)
    result = r.run(artifact(), cwd=tmp_path, policy=policy())
    assert result.metadata['failure_reason'] == 'capacity_unavailable' and result.metadata['retry_at'] > time.time()
    assert requests == [] and not (tmp_path / 'codex.calls').exists()
    again = r.run(artifact(), cwd=tmp_path, policy=policy())
    assert again.metadata['unified_execution_v1']['attempts'] == []


def test_native_permission_is_never_inferred_from_api_write_grants(rejection_server, tmp_path):
    url, _, responses = rejection_server
    responses.append((200, completion('local')[1], 0))
    result = runtime(tmp_path, url).run(artifact(), cwd=tmp_path, policy=policy(native_policy=None))
    assert result.exit_code == 0
    assert result.metadata['unified_execution_v1']['native_attempts'] == 0
    assert not (tmp_path / 'claude.calls').exists()


def test_readonly_native_profile_cannot_receive_write_policy_even_for_text_task(rejection_server, tmp_path):
    url, _, responses = rejection_server
    responses.append((200, completion('local')[1], 0))
    result = runtime(tmp_path, url).run(artifact(), cwd=tmp_path,
        policy=policy(native_policy=sdk.NativePolicy(permission_mode='workspace-write')))
    assert result.exit_code == 0 and result.metadata['profile'] == 'local'
    assert not (tmp_path / 'claude.calls').exists()


def test_capabilities_filter_native_without_advertising_api_contracts(rejection_server, tmp_path):
    url, _, responses = rejection_server
    responses.append((200, completion('local')[1], 0))
    r = runtime(tmp_path, url)
    assert 'unified_pools_v1' in r.capabilities
    assert 'conversation_turn_v1' not in r.capabilities and 'read_receipts_v1' not in r.capabilities
    result = r.run(artifact(), cwd=tmp_path, policy=policy(required_capabilities=frozenset({'read_receipts_v1'})))
    assert result.exit_code == 0 and not (tmp_path / 'claude.calls').exists()


def test_full_artifact_is_not_silently_sent_to_api(rejection_server, tmp_path):
    url, requests, _ = rejection_server
    r = runtime(tmp_path, url, order=('local',))
    result = r.run(artifact('full'), cwd=tmp_path, policy=policy(native_policy=None))
    assert result.exit_code != 0 and requests == []


def test_profile_attempt_cap_stops_before_next_dispatch(rejection_server, tmp_path):
    url, requests, _ = rejection_server
    result = runtime(tmp_path, url).run(artifact(), cwd=tmp_path,
        policy=policy(max_attempts=1, max_native_attempts=1))
    assert result.metadata['failure_reason'] == 'attempt_limit'
    assert requests == [] and not (tmp_path / 'codex.calls').exists()


def test_api_request_allowance_cannot_reset_between_profiles(rejection_server, tmp_path):
    url, requests, responses = rejection_server
    responses.append((429, b'limited', 0))
    r = runtime(tmp_path, url, order=('local',))
    c = replace(r.config, api_config=replace(r.config.api_config,
        profiles={**r.config.api_config.profiles, 'second': r.config.api_config.profiles['local']}),
        api_quota_keys={'local': 'l', 'second': 'second'}, pools={'agents': sdk.ProfilePool(('local', 'second'))})
    result = sdk.UnifiedRuntime(c).run(artifact(), cwd=tmp_path, policy=policy(native_policy=None,
        api_policy=sdk.RunPolicy(max_provider_requests=1, max_tool_calls=0)))
    assert result.exit_code != 0 and len(requests) == 1
    assert result.metadata['unified_execution_v1']['api_provider_requests'] == 1


def test_cancel_and_critical_selection_hook_stop_dispatch(rejection_server, tmp_path):
    url, requests, _ = rejection_server
    r = runtime(tmp_path, url)
    result = r.run(artifact(), cwd=tmp_path, policy=policy(), cancelled=lambda: True)
    assert result.exit_code == 130 and requests == []
    def fail(event):
        if event['event'] == 'profile_selected':
            raise RuntimeError('host hook')
    with pytest.raises(RuntimeError, match='host hook'):
        r.run(artifact(), cwd=tmp_path, policy=policy(), on_event=fail)
    assert not (tmp_path / 'claude.calls').exists()


def test_pool_deadline_includes_selection_hooks(rejection_server, tmp_path):
    url, requests, _ = rejection_server
    r = runtime(tmp_path, url)
    def slow(event):
        if event['event'] == 'profile_selected':time.sleep(.08)
    result = r.run(artifact(), cwd=tmp_path, policy=policy(timeout_s=.05), on_event=slow)
    assert result.timed_out and requests == []
    assert not (tmp_path / 'claude.calls').exists()


@pytest.mark.parametrize('failure', ['connection', 'http'])
def test_callback_transport_shaped_error_after_acceptance_cannot_authorize_switch(rejection_server, tmp_path, failure):
    import io
    import urllib.error
    url, requests, responses = rejection_server
    responses.extend([(200, completion('first')[1], 0), (200, completion('second')[1], 0)])
    r = runtime(tmp_path, url, order=('local',))
    config = replace(r.config, api_config=replace(r.config.api_config,
        profiles={**r.config.api_config.profiles, 'second': r.config.api_config.profiles['local']}),
        api_quota_keys={'local': 'l', 'second': 'second'}, pools={'agents': sdk.ProfilePool(('local', 'second'))})
    raised = False
    def cancelled():
        nonlocal raised
        if len(requests) == 1 and not raised:
            raised = True
            if failure == 'http':
                raise urllib.error.HTTPError(url, 429, 'callback', {}, io.BytesIO(b'callback failure'))
            raise urllib.error.URLError(ConnectionRefusedError('callback failure after accepted work'))
        return False
    result = sdk.UnifiedRuntime(config).run(artifact(), cwd=tmp_path, policy=policy(native_policy=None),
                                            cancelled=cancelled)
    assert result.exit_code != 0 and len(requests) == 1
    assert result.metadata['recovery_required']


@pytest.mark.parametrize('item', [
    {'type': 'agent_message', 'text': 'partial answer'}, {'type': 'reasoning', 'text': 'partial work'}])
def test_native_partial_model_work_is_not_unified_prework(rejection_server, tmp_path, item):
    url, requests, responses = rejection_server
    responses.append((200, completion('must not run')[1], 0))
    r = runtime(tmp_path, url, order=('codex', 'local'))
    executable = binary(tmp_path, 'partial-native', [
        {'type': 'item.completed', 'item': item},
        {'type': 'turn.failed', 'error': {'code': 'usage_limit_reached'}}], exit_code=1)
    native = replace(r.config.native_config, profiles={**r.config.native_config.profiles,
        'codex': replace(r.config.native_config.profiles['codex'], executable=executable)})
    result = sdk.UnifiedRuntime(replace(r.config, native_config=native)).run(
        artifact(), cwd=tmp_path, policy=policy())
    assert result.exit_code != 0 and requests == []
    assert result.metadata['recovery_required']
