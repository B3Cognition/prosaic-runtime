"""Opt-in native agent configuration, separate from API endpoint contracts."""
from dataclasses import dataclass, field, replace
import math
from pathlib import Path
import re

import yaml


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise ValueError(f'{name} must be nonempty text')


def _positive(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f'{name} must be finite and positive')


@dataclass(frozen=True)
class NativeProfile:
    backend: str
    model: str
    quota_key: str
    executable: str | None = None
    permission_mode: str = 'read-only'
    effort: str | None = None
    native_profile: str | None = None
    settings_file: str | None = None
    config_dir: str | None = None

    def __post_init__(self):
        if self.backend not in {'claude-code', 'codex-cli'}:
            raise ValueError('unsupported native backend')
        for name in ('model', 'quota_key'):
            _text(getattr(self, name), name)
        if self.permission_mode not in {'read-only', 'workspace-write'}:
            raise ValueError('unsupported native permission_mode')
        if self.effort not in {None, 'low', 'medium', 'high'}:
            raise ValueError('unsupported native effort')
        if self.native_profile is not None:
            if self.backend != 'codex-cli' or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_-]{0,127}', self.native_profile):
                raise ValueError('native_profile requires a Codex profile name')
        if self.settings_file is not None and self.backend != 'claude-code':
            raise ValueError('settings_file requires claude-code')
        for name in ('settings_file', 'config_dir', 'executable'):
            value = getattr(self, name)
            if value is not None:
                _text(value, name)
        if self.executable is None:
            object.__setattr__(self, 'executable', 'claude' if self.backend == 'claude-code' else 'codex')


@dataclass(frozen=True)
class ProfilePool:
    profiles: tuple[str, ...]
    cooldown_s: float = 300
    strategy: str = 'prefer-first-available'

    def __post_init__(self):
        if (not isinstance(self.profiles, tuple) or not 1 <= len(self.profiles) <= 16
                or any(not isinstance(p, str) or not p for p in self.profiles)
                or len(set(self.profiles)) != len(self.profiles)):
            raise ValueError('pool needs 1–16 distinct profile names')
        _positive(self.cooldown_s, 'cooldown_s')
        if self.strategy != 'prefer-first-available':
            raise ValueError('unsupported pool strategy')


@dataclass(frozen=True)
class NativePolicy:
    """Explicit host acceptance of vendor-owned native workspace permissions.

    This is not RunPolicy: no exact path grants, controlled tool receipts,
    request/token caps, or portable conversation guarantees are implied.
    """
    permission_mode: str = 'read-only'
    timeout_s: float = 120
    max_input_bytes: int = 262144
    max_output_bytes: int = 8388608

    def __post_init__(self):
        if self.permission_mode not in {'read-only', 'workspace-write'}:
            raise ValueError('unsupported native permission_mode')
        _positive(self.timeout_s, 'timeout_s')
        for name in ('max_input_bytes', 'max_output_bytes'):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f'{name} must be a positive integer')


@dataclass(frozen=True)
class NativeRuntimeConfig:
    profiles: dict[str, NativeProfile]
    pools: dict[str, ProfilePool] = field(default_factory=dict)
    default_profile: str | None = None
    default_pool: str | None = None
    routes: dict[str, str] = field(default_factory=dict)
    capacity_file: str | None = None

    def __post_init__(self):
        if (not isinstance(self.profiles, dict) or not self.profiles
                or any(not isinstance(v, NativeProfile) for v in self.profiles.values())):
            raise ValueError('native profiles must be a nonempty mapping')
        if not isinstance(self.pools, dict) or any(not isinstance(v, ProfilePool) for v in self.pools.values()):
            raise ValueError('native pools must be a mapping')
        if self.profiles.keys() & self.pools.keys():
            raise ValueError('profile and pool names must be distinct')
        for name in (*self.profiles, *self.pools):
            if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', name):
                raise ValueError('invalid native profile or pool name')
        if (self.default_profile is None) == (self.default_pool is None):
            raise ValueError('select exactly one default_profile or default_pool')
        if self.default_profile is not None and self.default_profile not in self.profiles:
            raise ValueError('unknown default profile')
        if self.default_pool is not None and self.default_pool not in self.pools:
            raise ValueError('unknown default pool')
        for pool in self.pools.values():
            if any(name not in self.profiles for name in pool.profiles):
                raise ValueError('pool references an unknown profile')
        if not isinstance(self.routes, dict) or any(
                not isinstance(k, str) or v not in self.profiles.keys() | self.pools.keys()
                for k, v in self.routes.items()):
            raise ValueError('invalid native route')
        if self.capacity_file is not None:
            _text(self.capacity_file, 'capacity_file')

    @classmethod
    def load(cls, path):
        path = Path(path)
        try:
            raw = yaml.safe_load(path.read_text(encoding='utf-8'))
            if not isinstance(raw, dict) or raw.get('kind') != 'native':
                raise ValueError('expected kind: native')
            if set(raw) - {'kind', 'profiles', 'pools', 'default_profile', 'default_pool', 'routes', 'capacity_file'}:
                raise ValueError('unknown native configuration keys')
            profiles = {}
            for name, value in raw['profiles'].items():
                profile = NativeProfile(**value)
                paths = {}
                for key in ('settings_file', 'config_dir'):
                    if getattr(profile, key) is not None:
                        paths[key] = str((path.resolve().parent / getattr(profile, key)).resolve())
                if '/' in profile.executable:
                    paths['executable'] = str((path.resolve().parent / profile.executable).resolve())
                profiles[name] = replace(profile, **paths)
            pools = {}
            for name, value in raw.get('pools', {}).items():
                if not isinstance(value, dict) or not isinstance(value.get('profiles'), list):
                    raise ValueError('pool profiles must be a list')
                pools[name] = ProfilePool(**(value | {'profiles': tuple(value['profiles'])}))
            capacity = raw.get('capacity_file')
            if capacity is not None:
                _text(capacity, 'capacity_file')
                capacity = str((path.resolve().parent / capacity).resolve())
            return cls(profiles, pools, raw.get('default_profile'), raw.get('default_pool'),
                       raw.get('routes', {}), capacity)
        except (yaml.YAMLError, TypeError, KeyError, AttributeError) as exc:
            raise ValueError(f'invalid native configuration: {exc}') from exc
