"""Ordered assignment selection; existing engines own execution and safety proof."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import time

from .admission import _inspection_artifact
from .artifacts import inspect_artifact
from .capacity import CapacityStore
from .native import NativeRuntime
from .native_config import NativeRuntimeConfig, ProfilePool
from .policy import BUILTIN_TOOLS, WRITE_TOOLS, requested_tools
from .runtime import ProsaicRuntime
from .types import Result
from .unified_config import PoolPolicy, UnifiedRuntimeConfig


class UnifiedRuntime:
    """Stateless assignment pooling, without conversation or API authority claims.

    Native inner provider calls remain opaque. API counters are separately
    bounded across API candidates. Hosts own writable-workspace isolation.
    """
    capabilities = frozenset({'unified_pools_v1'})

    def __init__(self, config, *, source='.prosaic', executable='prosaic', capacity=None):
        if not isinstance(config, UnifiedRuntimeConfig):
            raise ValueError('UnifiedRuntime requires UnifiedRuntimeConfig')
        self.config = deepcopy(config)
        self.source, self.executable = Path(source), executable
        self.capacity = capacity if capacity is not None else CapacityStore(config.capacity_file)

    def run(self, artifact, arguments='', *, cwd='.', policy=None, pool=None,
            on_event=None, cancelled=None, env=None):
        if not isinstance(policy, PoolPolicy):
            raise ValueError('unified execution requires explicit PoolPolicy')
        started = time.monotonic()
        policy, config = deepcopy(policy), deepcopy(self.config)
        PoolPolicy.__post_init__(policy)
        UnifiedRuntimeConfig.__post_init__(config)
        deadline = started + policy.timeout_s
        pool = config.default_pool if pool is None else pool
        if pool not in config.pools:
            raise ValueError('unknown unified pool')
        selection = config.pools[pool]
        native = config.native_config.profiles if config.native_config else {}
        api = config.api_config.profiles if config.api_config else {}
        attempts, resets = [], []
        native_count = api_requests = api_tools = api_tokens = known_tokens = 0
        usage_complete = api_usage_complete = True
        native_unknown = api_unknown = False

        def emit(event, **fields):
            if on_event is not None:
                on_event({'event': event, 'pool': pool, **fields})

        def finish(result):
            result.token_usage = known_tokens if usage_complete else None
            result.metadata.update(pool=pool, token_usage_status='reported' if usage_complete else 'unknown',
                unified_execution_v1={'attempts': attempts, 'native_attempts': None if native_unknown else native_count,
                    'api_provider_requests': None if api_unknown else api_requests,
                    'api_tool_calls': None if api_unknown else api_tools,
                    'api_reported_tokens': api_tokens, 'api_usage_complete': api_usage_complete,
                    'known_tokens': known_tokens, 'usage_complete': usage_complete,
                    'duration_ms': (time.monotonic() - started) * 1000})
            result.metadata.setdefault('recovery_required', False)
            if attempts:
                result.metadata['profile'] = attempts[-1]['profile']
            emit('completed', exit_code=result.exit_code)
            return result

        def stopped():
            if cancelled is not None and cancelled():
                return Result(130, '', 'invocation cancelled', metadata={'failure_reason': 'cancelled'})
            if time.monotonic() >= deadline:
                return Result(1, '', 'pool deadline exceeded', timed_out=True, metadata={'failure_reason': 'timeout'})
            return None

        early = stopped()
        if early is not None:
            return finish(early)
        if isinstance(artifact, str):
            artifact = inspect_artifact(artifact, self.source, executable=self.executable,
                timeout_s=min(30, max(.001, deadline - time.monotonic())))
        artifact = _inspection_artifact(artifact)
        if not isinstance(arguments, str):
            raise ValueError('unified arguments must be text')
        tier = artifact.frontmatter.get('model_tier')
        if tier is not None and not isinstance(tier, str):
            raise ValueError('model_tier must be text')
        declaration = artifact.frontmatter.get('tools')
        tools = BUILTIN_TOOLS if declaration == 'full' else requested_tools(declaration)
        workspace = Path(cwd).resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError('unified cwd must be a directory')
        emit('started')
        last_skip = 'no_eligible_profile'
        for name in selection.profiles:
            early = stopped()
            if early is not None:
                return finish(early)
            if len(attempts) >= policy.max_attempts:
                return finish(Result(1, '', 'pool attempt allowance exhausted', metadata={'failure_reason': 'attempt_limit'}))
            is_native = name in native
            backend_policy = policy.native_policy if is_native else policy.api_policy
            capabilities = NativeRuntime.capabilities if is_native else ProsaicRuntime.capabilities
            skip = None
            if backend_policy is None:
                skip = 'policy_missing'
            elif not policy.required_capabilities <= capabilities:
                skip = 'capability_unavailable'
            elif is_native and (tools - BUILTIN_TOOLS or (
                    backend_policy.permission_mode == 'workspace-write' and native[name].permission_mode != 'workspace-write')
                    or (tools & WRITE_TOOLS and (
                    backend_policy.permission_mode != 'workspace-write' or native[name].permission_mode != 'workspace-write'))):
                skip = 'permission_unavailable'
            elif not is_native and declaration == 'full':
                skip = 'capability_unavailable'
            elif is_native and native_count >= policy.max_native_attempts:
                skip = 'native_attempt_limit'
            elif not is_native and api_requests >= backend_policy.max_provider_requests:
                skip = 'provider_request_limit'
            elif not is_native and backend_policy.max_reported_tokens is not None and not api_usage_complete:
                skip = 'usage_unknown'
            elif not is_native and backend_policy.max_reported_tokens is not None and api_tokens >= backend_policy.max_reported_tokens:
                skip = 'token_limit'
            if skip is not None:
                last_skip = skip
                emit('profile_skipped', profile=name, reason=skip)
                continue
            quota = native[name].quota_key if is_native else config.api_quota_keys[name]
            try:
                state = self.capacity.status(quota, deadline=deadline, cancelled=cancelled)
            except (TimeoutError, InterruptedError) as exc:
                reason = 'cancelled' if isinstance(exc, InterruptedError) else 'timeout'
                return finish(Result(130 if reason == 'cancelled' else 1, '', reason,
                    timed_out=reason == 'timeout', metadata={'failure_reason': reason}))
            if state['state'] == 'cooling_down':
                resets.append(state['until'])
                last_skip = 'capacity_unavailable'
                emit('profile_skipped', profile=name, reason='capacity_unavailable')
                continue
            emit('profile_selected', profile=name, backend=native[name].backend if is_native else api[name].provider)
            early = stopped()
            if early is not None:
                return finish(early)
            remaining = min(backend_policy.timeout_s, deadline - time.monotonic())
            entry = {'profile': name, 'kind': 'native' if is_native else 'api',
                     'backend': native[name].backend if is_native else api[name].provider,
                     'model': native[name].model if is_native else api[name].model}
            attempts.append(entry)
            try:
                if is_native:
                    delegate_pool = '_delegate' if name != '_delegate' else '_delegate2'
                    delegate = NativeRuntimeConfig({name: native[name]},
                        {delegate_pool: ProfilePool((name,), cooldown_s=selection.cooldown_s)}, default_pool=delegate_pool)
                    result = NativeRuntime(delegate, source=self.source, executable=self.executable,
                        capacity=self.capacity).run(artifact, arguments, cwd=workspace,
                            policy=replace(backend_policy, timeout_s=remaining), pool=delegate_pool,
                            cancelled=cancelled, env=env)
                    info = result.metadata.get('native_execution_v1', {})
                    native_count += len(info.get('attempts', []))
                    partial = info.get('known_tokens', 0)
                else:
                    # Pool selection, rather than the backend's default/tier route,
                    # explicitly owns this endpoint choice. The prose stays intact.
                    routes = {key: name for key in config.api_config.routes}
                    if tier is not None:
                        routes[tier] = name
                    delegate = replace(config.api_config, default_profile=name, routes=routes)
                    effective = replace(backend_policy, timeout_s=remaining,
                        max_provider_requests=backend_policy.max_provider_requests - api_requests,
                        max_tool_calls=max(0, backend_policy.max_tool_calls - api_tools),
                        max_reported_tokens=(None if backend_policy.max_reported_tokens is None else
                                             max(0, backend_policy.max_reported_tokens - api_tokens)))
                    result = ProsaicRuntime(delegate, source=self.source, executable=self.executable).run(
                        artifact, arguments, cwd=workspace, policy=effective, cancelled=cancelled, env=env)
                    info = result.metadata['invocation_budgets_v1']
                    api_requests += info['provider_requests']
                    api_tools += info['tool_calls']
                    partial = max(info['reported_tokens'], result.token_usage or 0,
                                  result.metadata.get('reported_token_usage') or 0)
                    api_tokens += partial
                    api_usage_complete &= result.token_usage is not None
            except Exception:
                if is_native:
                    native_unknown = True
                else:
                    api_unknown = True
                    api_usage_complete = False
                usage_complete = False
                entry.update(exit_code=1, failure_reason='execution_uncertain', safe_to_fallback=False)
                return finish(Result(1, '', 'delegate execution uncertain',
                    metadata={'failure_reason': 'execution_uncertain', 'recovery_required': True}))
            known_tokens += partial
            usage_complete &= result.token_usage is not None
            proof = result.metadata.get('prework_failure_v1', {})
            safe = (result.exit_code != 0 and proof.get('safe_to_fallback') is True
                    and proof.get('reason') in {'rate_limit', 'usage_limit', 'unavailable', 'capacity_unavailable'})
            entry.update(exit_code=result.exit_code, failure_reason=result.metadata.get('failure_reason'),
                         safe_to_fallback=safe, token_usage=result.token_usage)
            early = stopped()
            if early is not None:
                early.metadata['recovery_required'] = not safe
                return finish(early)
            if result.exit_code == 0:
                return finish(result)
            if not safe:
                if result.metadata.get('work_started') or not is_native and (info['provider_requests'] or info['tool_calls']):
                    result.metadata['recovery_required'] = True
                return finish(result)
            reason = proof['reason']
            until = result.metadata.get('retry_at')
            if type(until) not in (int, float) or until <= time.time():
                until = time.time() + selection.cooldown_s
            try:
                self.capacity.mark_unavailable(quota, reason=reason if reason != 'capacity_unavailable' else 'unavailable',
                    until=until, deadline=deadline, cancelled=cancelled)
            except (TimeoutError, InterruptedError) as exc:
                reason = 'cancelled' if isinstance(exc, InterruptedError) else 'timeout'
                return finish(Result(130 if reason == 'cancelled' else 1, '', reason,
                    timed_out=reason == 'timeout', metadata={'failure_reason': reason}))
            resets.append(until)
            emit('profile_fallback', profile=name, reason=proof['reason'])
        reason = 'capacity_unavailable' if resets else last_skip
        metadata = {'failure_reason': reason, 'recovery_required': False}
        if resets:
            metadata['retry_at'] = min(resets)
        return finish(Result(1, '', 'no eligible pool profile currently available', metadata=metadata))
