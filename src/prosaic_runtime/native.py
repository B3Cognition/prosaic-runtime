"""Native agent execution extension with capacity-aware profile selection."""
from dataclasses import replace
import os
from pathlib import Path
import time

from .admission import _inspection_artifact
from .artifacts import inspect_artifact
from .capacity import CapacityStore
from .native_backends import execute_native
from .native_config import NativePolicy, NativeRuntimeConfig
from .policy import BUILTIN_TOOLS, WRITE_TOOLS, requested_tools
from .types import Result


class NativeRuntime:
    """One native assignment, optionally falling back before observed work.

    NativePolicy explicitly accepts the vendor's permissions and trusted local
    configuration. This is deliberately not a ProsaicRuntime subclass: API tool
    grants, receipts, accounting and conversation contracts do not apply.
    Instances may execute concurrently; hosts own workspace write isolation.
    """
    capabilities = frozenset({'native_profiles_v1', 'profile_pools_v1'})

    def __init__(self, config, *, source='.prosaic', executable='prosaic', capacity=None):
        if not isinstance(config, NativeRuntimeConfig):
            raise ValueError('NativeRuntime requires NativeRuntimeConfig')
        # Own mutable operator mappings rather than retaining a caller alias.
        self.config = replace(config, profiles=dict(config.profiles), pools=dict(config.pools),
                              routes=dict(config.routes))
        self.source = Path(source)
        self.executable = executable
        self.capacity = capacity if capacity is not None else CapacityStore(config.capacity_file)

    def run(self, artifact, arguments='', *, cwd='.', policy=None, profile=None,
            pool=None, on_event=None, cancelled=None, env=None):
        if not isinstance(policy, NativePolicy):
            raise ValueError('native execution requires explicit NativePolicy; RunPolicy is not supported')
        if profile is not None and pool is not None:
            raise ValueError('select profile or pool, not both')
        started = time.monotonic()
        deadline = started + policy.timeout_s
        def emit(event, **data):
            if on_event is not None:
                on_event({'event': event, **data})
        def stopped():
            if cancelled is not None and cancelled():
                return Result(130, '', 'invocation cancelled', metadata={'failure_reason': 'cancelled'})
            if time.monotonic() >= deadline:
                return Result(1, '', 'invocation deadline exceeded', timed_out=True,
                              metadata={'failure_reason': 'timeout'})
            return None
        early = stopped()
        if early is not None:
            return early
        if isinstance(artifact, str):
            artifact = inspect_artifact(artifact, self.source, executable=self.executable,
                                        timeout_s=min(30, max(.001, deadline - time.monotonic())))
        artifact = _inspection_artifact(artifact)
        effort = artifact.frontmatter.get('effort')
        if effort not in {None, 'low', 'medium', 'high'}:
            raise ValueError('unsupported native artifact effort')
        declaration = artifact.frontmatter.get('tools')
        # Native coding agents supply the full loop; API execution continues to
        # reject this declaration. Both host and operator must explicitly grant
        # workspace writes before a full native agent can be dispatched.
        tools = BUILTIN_TOOLS if declaration == 'full' else requested_tools(declaration)
        if tools - BUILTIN_TOOLS:
            raise ValueError('custom API tools are not supported by native execution')
        if tools & WRITE_TOOLS and policy.permission_mode != 'workspace-write':
            raise ValueError('artifact requests write permission')
        tier = artifact.frontmatter.get('model_tier')
        if profile is None and pool is None:
            if tier is not None:
                if not isinstance(tier, str) or tier not in self.config.routes:
                    raise ValueError(f'no native route for model_tier: {tier}')
                selected = self.config.routes[tier]
            else:
                selected = self.config.default_profile or self.config.default_pool
            if selected in self.config.profiles:
                profile = selected
            else:
                pool = selected
        if profile is not None:
            if profile not in self.config.profiles:
                raise ValueError('unknown native profile')
            names, cooldown = (profile,), 300
        else:
            if pool not in self.config.pools:
                raise ValueError('unknown native pool')
            selection = self.config.pools[pool]
            names, cooldown = selection.profiles, selection.cooldown_s
        names = tuple(name for name in names if policy.permission_mode == 'read-only'
                      or self.config.profiles[name].permission_mode == 'workspace-write')
        if not names:
            raise ValueError('no profile can grant the requested native permission')
        prompt = artifact.render(arguments)
        if len(prompt.encode('utf-8')) > policy.max_input_bytes:
            raise ValueError('artifact and arguments exceed max_input_bytes')
        workspace = Path(cwd).resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError('native cwd must be a directory')
        attempts = []
        known_tokens = 0
        complete_usage = True

        def finish(result):
            result.token_usage = known_tokens if complete_usage else None
            result.metadata.update(artifact_id=artifact.id, artifact_sha256=artifact.digest,
                pool=pool, token_usage_status='reported' if complete_usage else 'unknown',
                permission_owner='native', cost_status='unavailable',
                native_execution_v1={'attempts': attempts, 'known_tokens': known_tokens,
                                     'duration_ms': (time.monotonic() - started) * 1000})
            emit('completed', exit_code=result.exit_code, token_usage=result.token_usage)
            return result

        def capacity_interrupted(exc, previous=None):
            reason = 'cancelled' if isinstance(exc, InterruptedError) else 'timeout'
            metadata = dict(previous.metadata) if previous is not None else {}
            metadata.update(failure_reason=reason, safe_to_fallback=False,
                recovery_required=metadata.get('effects_uncertain', False))
            return finish(Result(130 if reason == 'cancelled' else 1, '', str(exc),
                                 timed_out=reason == 'timeout', metadata=metadata))

        emit('started', artifact_id=artifact.id, artifact_sha256=artifact.digest, pool=pool)
        for name in names:
            early = stopped()
            if early is not None:
                return finish(early)
            endpoint = self.config.profiles[name]
            try:
                available = self.capacity.available(endpoint.quota_key, deadline=deadline, cancelled=cancelled)
            except (TimeoutError, InterruptedError) as exc:
                return capacity_interrupted(exc)
            if not available:
                emit('profile_skipped', profile=name, reason='capacity_unavailable')
                continue
            if endpoint.effort is None and effort is not None:
                endpoint = replace(endpoint, effort=effort)
            emit('profile_selected', profile=name, backend=endpoint.backend, model=endpoint.model)
            early = stopped()
            if early is not None:
                return finish(early)
            result = execute_native(endpoint, prompt, cwd=workspace, policy=policy, deadline=deadline,
                env=dict(os.environ if env is None else env), cancelled=cancelled, on_event=on_event)
            result.metadata['profile'] = name
            reason = result.metadata.get('failure_reason')
            attempts.append({'profile': name, 'backend': endpoint.backend, 'model': endpoint.model,
                'exit_code': result.exit_code, 'failure_reason': reason, 'token_usage': result.token_usage,
                'effects_uncertain': result.metadata['effects_uncertain'],
                'work_started': result.metadata.get('work_started', False)})
            if result.token_usage is None:
                complete_usage = False
            else:
                known_tokens += result.token_usage
            if reason in {'usage_limit', 'rate_limit', 'unavailable'}:
                until = result.metadata.get('retry_at')
                if until is None or until <= time.time():
                    until = time.time() + cooldown
                try:
                    self.capacity.mark_unavailable(endpoint.quota_key, reason=reason, until=until,
                                                   deadline=deadline, cancelled=cancelled)
                except (TimeoutError, InterruptedError) as exc:
                    return capacity_interrupted(exc, result)
            if result.exit_code == 0:
                early = stopped()
                if early is not None:
                    early.metadata['effects_uncertain'] = result.metadata['effects_uncertain']
                    early.metadata['recovery_required'] = result.metadata['effects_uncertain']
                    return finish(early)
                return finish(result)
            if not result.metadata['safe_to_fallback']:
                result.metadata['recovery_required'] = result.metadata['effects_uncertain']
                return finish(result)
            emit('profile_fallback', profile=name, reason=reason)
        try:
            resets = [self.capacity.status(self.config.profiles[name].quota_key,
                deadline=deadline, cancelled=cancelled).get('until') for name in names]
        except (TimeoutError, InterruptedError) as exc:
            return capacity_interrupted(exc)
        metadata = {'failure_reason': 'capacity_unavailable', 'safe_to_fallback': False,
                    'effects_uncertain': False, 'recovery_required': False,
                    'work_started': any(a['work_started'] for a in attempts)}
        if not metadata['work_started']:
            metadata['prework_failure_v1'] = {'safe_to_fallback': True, 'reason': 'capacity_unavailable'}
        if any(reset is not None for reset in resets):
            metadata['retry_at'] = min(reset for reset in resets if reset is not None)
        return finish(Result(1, '', 'no eligible native profile currently has capacity', metadata=metadata))
