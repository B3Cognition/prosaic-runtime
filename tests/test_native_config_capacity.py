import json
from concurrent.futures import ThreadPoolExecutor

import pytest


def test_native_configuration_is_opt_in_and_validates_references(tmp_path):
    from prosaic_runtime import NativeRuntimeConfig
    path = tmp_path / 'native.yml'
    path.write_text('''kind: native
profiles:
  claude:
    backend: claude-code
    model: sonnet
    quota_key: claude-account
  codex:
    backend: codex-cli
    model: example-model
    quota_key: codex-account
pools:
  coding:
    profiles: [claude, codex]
default_pool: coding
routes: {strong: coding}
''')
    config = NativeRuntimeConfig.load(path)
    assert config.profiles['claude'].executable == 'claude'
    assert config.pools['coding'].profiles == ('claude', 'codex')
    assert config.routes['strong'] == 'coding'
    path.write_text(path.read_text().replace('[claude, codex]', '[claude, missing]'))
    with pytest.raises(ValueError, match='unknown profile'):
        NativeRuntimeConfig.load(path)


@pytest.mark.parametrize('changes', [
    {'backend': 'shell'}, {'quota_key': ''}, {'model': ''},
    {'permission_mode': 'unrestricted'}, {'effort': 'bogus'},
    {'native_profile': '-flag'}, {'settings_file': 'settings.json'},
])
def test_invalid_native_profile(changes):
    from prosaic_runtime import NativeProfile
    with pytest.raises(ValueError):
        NativeProfile(**({'backend': 'codex-cli', 'model': 'test', 'quota_key': 'account'} | changes))


def test_capacity_unknown_shared_cooldown_and_reset():
    from prosaic_runtime import CapacityStore
    store = CapacityStore()
    assert store.status('account', now=10)['state'] == 'unknown'
    store.mark_unavailable('account', reason='usage_limit', until=30)
    assert not store.available('account', now=20)
    # A shorter observation cannot accidentally undo a longer cooldown.
    store.mark_unavailable('account', reason='rate_limit', until=25)
    assert not store.available('account', now=29)
    assert store.available('account', now=31)
    store.mark_available('account')
    assert store.status('account', now=31)['state'] == 'available'


def test_file_capacity_updates_are_atomic_between_instances(tmp_path):
    from prosaic_runtime import CapacityStore
    path = tmp_path / 'capacity.json'
    def update(i):
        CapacityStore(path).mark_unavailable(str(i), reason='rate_limit', until=100)
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(update, range(32)))
    store = CapacityStore(path)
    assert all(not store.available(str(i), now=50) for i in range(32))
    assert len(json.loads(path.read_text())['buckets']) == 32
    assert path.stat().st_mode & 0o777 == 0o600


def test_corrupt_capacity_is_not_silently_treated_as_available(tmp_path):
    from prosaic_runtime import CapacityStore
    path = tmp_path / 'capacity.json'
    path.write_text('{bad')
    with pytest.raises(ValueError, match='capacity'):
        CapacityStore(path).available('account')


def test_capacity_lock_wait_obeys_deadline_and_cancellation(tmp_path):
    import fcntl
    import time
    from prosaic_runtime import CapacityStore
    path = tmp_path / 'capacity.json'
    with open(str(path) + '.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        store = CapacityStore(path)
        with pytest.raises(TimeoutError):
            store.available('account', deadline=time.monotonic() + .05)
        with pytest.raises(InterruptedError):
            store.available('account', cancelled=lambda: True)
