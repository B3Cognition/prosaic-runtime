"""Explicit host configuration and authority for mixed execution pools."""
from dataclasses import dataclass
import math
from pathlib import Path
import re

import yaml

from .config import RuntimeConfig
from .native_config import NativePolicy, NativeRuntimeConfig, ProfilePool
from .policy import RunPolicy


def _name(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', value)


@dataclass(frozen=True)
class PoolPolicy:
    """Native permissions and API grants are independent, explicit contracts.

    API counters apply across all API members, not native inner request loops.
    Omitting a backend's policy excludes that backend from selection.
    """
    timeout_s: float
    max_attempts: int
    max_native_attempts: int
    native_policy: NativePolicy | None = None
    api_policy: RunPolicy | None = None
    required_capabilities: frozenset[str] = frozenset()

    def __post_init__(self):
        if type(self.timeout_s) not in (int, float) or not math.isfinite(self.timeout_s) or self.timeout_s <= 0:
            raise ValueError('pool timeout must be finite and positive')
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 16:
            raise ValueError('pool max_attempts must be 1..16')
        if type(self.max_native_attempts) is not int or not 0 <= self.max_native_attempts <= self.max_attempts:
            raise ValueError('pool max_native_attempts must be 0..max_attempts')
        if self.native_policy is not None and not isinstance(self.native_policy, NativePolicy):
            raise ValueError('native_policy must be NativePolicy')
        if self.api_policy is not None:
            if not isinstance(self.api_policy, RunPolicy):
                raise ValueError('api_policy must be RunPolicy')
            if self.api_policy.max_provider_requests is None or self.api_policy.max_tool_calls is None:
                raise ValueError('API provider-request and tool-call budgets must be explicit')
        if (not isinstance(self.required_capabilities, frozenset)
                or any(not _name(c) for c in self.required_capabilities)):
            raise ValueError('required_capabilities must contain bounded names')


@dataclass(frozen=True)
class UnifiedRuntimeConfig:
    native_config: NativeRuntimeConfig | None
    api_config: RuntimeConfig | None
    pools: dict[str, ProfilePool]
    default_pool: str
    api_quota_keys: dict[str, str]
    capacity_file: str | None = None

    def __post_init__(self):
        if self.native_config is not None and not isinstance(self.native_config, NativeRuntimeConfig):
            raise ValueError('native_config must be NativeRuntimeConfig')
        if self.api_config is not None and not isinstance(self.api_config, RuntimeConfig):
            raise ValueError('api_config must be RuntimeConfig')
        native = self.native_config.profiles if self.native_config else {}
        api = self.api_config.profiles if self.api_config else {}
        if not native and not api:
            raise ValueError('unified configuration needs execution profiles')
        if native.keys() & api.keys():
            raise ValueError('native and API profile aliases must be distinct')
        if (not isinstance(self.pools, dict) or not self.pools
                or any(not isinstance(p, ProfilePool) for p in self.pools.values())):
            raise ValueError('unified pools must be a nonempty mapping')
        if any(not _name(n) for n in (*native, *api, *self.pools)):
            raise ValueError('invalid unified profile or pool name')
        if self.pools.keys() & (native.keys() | api.keys()):
            raise ValueError('pool and profile names must be distinct')
        if self.default_pool not in self.pools:
            raise ValueError('unknown unified default pool')
        for pool in self.pools.values():
            if any(name not in native.keys() | api.keys() for name in pool.profiles):
                raise ValueError('unified pool references an unknown profile')
        if (not isinstance(self.api_quota_keys, dict) or self.api_quota_keys.keys() != api.keys()
                or any(not isinstance(v, str) or not v.strip() or '\x00' in v for v in self.api_quota_keys.values())):
            raise ValueError('explicit quota keys required for every API profile')
        if self.capacity_file is not None and (not isinstance(self.capacity_file, str)
                or not self.capacity_file.strip() or '\x00' in self.capacity_file):
            raise ValueError('invalid unified capacity file')

    @classmethod
    def load(cls, path):
        path = Path(path).resolve()
        try:
            raw = yaml.safe_load(path.read_text(encoding='utf-8'))
            if (not isinstance(raw, dict) or raw.get('kind') != 'unified' or set(raw) - {
                    'kind', 'native_config', 'api_config', 'pools', 'default_pool', 'api_quota_keys', 'capacity_file'}):
                raise ValueError('expected kind: unified and known keys')
            def reference(key):
                value = raw.get(key)
                if not isinstance(value, str) or not value.strip() or '\x00' in value:
                    raise ValueError('configuration reference must be a path')
                return (path.parent / value).resolve()
            native = NativeRuntimeConfig.load(reference('native_config')) if raw.get('native_config') is not None else None
            api = RuntimeConfig.load(reference('api_config')) if raw.get('api_config') is not None else None
            pools = {}
            for name, value in raw['pools'].items():
                if not isinstance(value, dict) or not isinstance(value.get('profiles'), list):
                    raise ValueError('pool profiles must be a list')
                pools[name] = ProfilePool(**(value | {'profiles': tuple(value['profiles'])}))
            capacity = str(reference('capacity_file')) if raw.get('capacity_file') is not None else None
            return cls(native, api, pools, raw['default_pool'], raw.get('api_quota_keys', {}), capacity)
        except (OSError, yaml.YAMLError, KeyError, TypeError, ValueError, AttributeError) as exc:
            raise ValueError(f'Invalid unified configuration {path}: {exc}') from exc
