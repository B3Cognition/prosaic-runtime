from dataclasses import replace

import pytest
import prosaic_runtime as sdk


def classes():
    assert hasattr(sdk, 'PoolPolicy'), 'missing explicit unified PoolPolicy'
    assert hasattr(sdk, 'UnifiedRuntimeConfig'), 'missing unified configuration'
    return sdk.PoolPolicy, sdk.UnifiedRuntimeConfig


def config():
    _, cls = classes()
    return cls(sdk.NativeRuntimeConfig({'claude': sdk.NativeProfile('claude-code', 'sonnet', 'account')},
                                      default_profile='claude'),
        sdk.RuntimeConfig({'local': sdk.EndpointConfig('http://127.0.0.1:9/v1', 'local')}, {}, 'local'),
        {'agents': sdk.ProfilePool(('claude', 'local'))}, 'agents', {'local': 'local-service'})


def test_unified_config_owns_distinct_backend_names_and_explicit_quota_keys():
    c = config()
    assert c.default_pool == 'agents'
    with pytest.raises(ValueError, match='quota'):
        replace(c, api_quota_keys={})
    with pytest.raises(ValueError, match='distinct|collision'):
        replace(c, api_config=sdk.RuntimeConfig({'claude': c.api_config.profiles['local']}, {}, 'claude'),
                api_quota_keys={'claude': 'account'})
    with pytest.raises(ValueError, match='profile'):
        replace(c, pools={'agents': sdk.ProfilePool(('missing',))})


@pytest.mark.parametrize('values', [dict(timeout_s=True), dict(timeout_s=float('inf')),
    dict(max_attempts=0), dict(max_native_attempts=True), dict(max_native_attempts=3),
    dict(required_capabilities=frozenset({'bad capability'})),
    dict(api_policy=sdk.RunPolicy()), dict(native_policy=sdk.RunPolicy()),
    dict(api_policy=sdk.RunPolicy(max_provider_requests=2))])
def test_pool_policy_rejects_implicit_api_caps_and_invalid_authority(values):
    cls, _ = classes()
    with pytest.raises(ValueError):
        cls(**(dict(timeout_s=60, max_attempts=2, max_native_attempts=2) | values))


def test_policy_requires_explicit_backend_grants_but_allows_zero_counters():
    cls, _ = classes()
    p = cls(timeout_s=60, max_attempts=2, max_native_attempts=0,
        api_policy=sdk.RunPolicy(max_provider_requests=0, max_tool_calls=0))
    assert p.native_policy is None


def test_unified_file_loads_existing_configs_relative_to_its_own_directory(tmp_path):
    _, cls = classes()
    (tmp_path / 'native.yml').write_text('kind: native\nprofiles:\n  claude:\n    backend: claude-code\n    model: sonnet\n    quota_key: account\ndefault_profile: claude\n')
    (tmp_path / 'api.yml').write_text('profiles:\n  local:\n    base_url: http://127.0.0.1:9/v1\n    model: local\ndefault_profile: local\n')
    path = tmp_path / 'unified.yml'
    path.write_text('kind: unified\nnative_config: native.yml\napi_config: api.yml\napi_quota_keys: {local: local-service}\npools:\n  agents:\n    profiles: [claude, local]\ndefault_pool: agents\ncapacity_file: capacity.json\n')
    c = cls.load(path)
    assert c.pools['agents'].profiles == ('claude', 'local')
    assert c.capacity_file == str(tmp_path / 'capacity.json')
    path.write_text(path.read_text() + 'unrecognized: true\n')
    with pytest.raises(ValueError, match='unified'):
        cls.load(path)


def test_cli_does_not_guess_pool_permissions_or_budgets(tmp_path, capsys):
    from prosaic_runtime.cli import main
    path = tmp_path / 'unified.yml'
    path.write_text('kind: unified\n')
    assert main(['task', '--config', str(path), '--quiet']) == 2
    assert 'UnifiedRuntime' in capsys.readouterr().err
