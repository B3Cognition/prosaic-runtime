"""Trusted host adaptation around an unmodified Runtime and one shared allowance.

ALWAYS dispatch through execute and retain every attempt's evidence.
NEVER broaden host policy or manufacture successful execution from a failed call.
"""
from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import math
import re
import time

from .runtime import ProsaicRuntime
from .lifecycle import tracked
from .policy import RunPolicy
from .types import Result
from .budgets import InvocationBudgetExceeded


class AdapterBoundary(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class _Attempts:
    def __init__(self, policy, maximum, deadline, cancelled):
        self.policy, self.maximum, self.deadline, self.cancelled = policy, maximum, deadline, cancelled
        self.records = []
        self.requests = self.tools = self.tokens = 0
        self.complete = True
        self.cost = 0.0
        self.last = None

    def remaining(self, timeout_s=None, allowed_tools=None, max_tool_rounds=None, max_input_bytes=None):
        if self.cancelled():
            raise AdapterBoundary('adapter_cancelled')
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise AdapterBoundary('adapter_timeout')
        if len(self.records) >= self.maximum:
            raise AdapterBoundary('adapter_attempt_limit')
        policy = self.policy
        values = {}
        for name, consumed in (('max_provider_requests', self.requests), ('max_tool_calls', self.tools), ('max_reported_tokens', self.tokens)):
            cap = getattr(policy, name)
            if cap is not None:
                if name == 'max_reported_tokens' and not self.complete:
                    raise InvocationBudgetExceeded('usage_unknown')
                if consumed >= cap and name != 'max_tool_calls':
                    raise InvocationBudgetExceeded('token_limit' if name == 'max_reported_tokens' else 'provider_request_limit')
                values[name] = max(0, cap - consumed)
        if allowed_tools is not None:
            grant = frozenset(allowed_tools)
            if grant - policy.allowed_tools:
                raise ValueError('adapter cannot broaden tool grants')
            values['allowed_tools'] = grant
        for name, value in (('max_tool_rounds', max_tool_rounds), ('max_input_bytes', max_input_bytes)):
            if value is not None:
                if type(value) is not int or value <= 0:
                    raise ValueError('adapter bounds must be positive integers')
                values[name] = min(getattr(policy, name), value)
        if timeout_s is not None:
            if not isinstance(timeout_s, (float, int)) or isinstance(timeout_s, bool) or not math.isfinite(timeout_s) or timeout_s <= 0:
                raise ValueError('adapter timeout must be finite and positive')
            remaining = min(remaining, timeout_s)
        return replace(policy, timeout_s=remaining, **values)

    def add(self, result, arguments):
        if type(result) is not Result:
            raise ValueError('adapter delegate must return Result')
        owned = deepcopy(result)
        info = owned.metadata.get('invocation_budgets_v1', {})
        requests = info.get('provider_requests', 1)
        tools = info.get('tool_calls', 0)
        if any(type(value) is not int or value < 0 for value in (requests, tools)):
            raise ValueError('invalid delegate allowance evidence')
        usage = owned.token_usage
        if usage is not None and (type(usage) is not int or usage < 0):
            raise ValueError('invalid delegate usage evidence')
        complete = info.get('usage_complete', usage is not None or requests == 0)
        if type(complete) is not bool:
            raise ValueError('invalid delegate usage completeness')
        self.complete &= complete and (usage is not None or requests == 0)
        reported = info.get('reported_tokens', usage or 0)
        if type(reported) is not int or reported < 0:
            raise ValueError('invalid partial usage evidence')
        known = max(reported, usage or 0)
        self.tokens += known
        self.requests += requests
        self.tools += tools
        if not isinstance(owned.cost_usd, (int, float)) or not math.isfinite(owned.cost_usd) or owned.cost_usd < 0:
            raise ValueError('invalid delegate cost evidence')
        self.cost += owned.cost_usd
        self.last = owned
        self.records.append({'index': len(self.records) + 1, 'exit_code': owned.exit_code,
            'timed_out': owned.timed_out, 'token_usage': usage, 'known_tokens': known, 'provider_requests': requests,
            'tool_calls': tools, 'arguments_sha256': hashlib.sha256(arguments.encode()).hexdigest()})

    def finish(self, result, descriptor):
        if type(result) is not Result:
            result = Result(1, '', 'adapter returned an invalid result', metadata={'failure_reason': 'adapter_result_mismatch'})
        if result.exit_code == 0 and (self.last is None or self.last.exit_code != 0):
            result = Result(1, '', 'adapter cannot admit a failed execution', metadata={'failure_reason': 'adapter_result_mismatch'})
        reason = None
        for name, value, code in (('max_provider_requests', self.requests, 'provider_request_limit'),
                                  ('max_tool_calls', self.tools, 'tool_call_limit'),
                                  ('max_reported_tokens', self.tokens, 'token_limit')):
            cap = getattr(self.policy, name)
            if cap is not None and value > cap:
                reason = code
        if self.policy.max_reported_tokens is not None and not self.complete:
            reason = 'usage_unknown'
        if result.exit_code == 0 and self.cancelled():
            result = Result(130, '', 'invocation cancelled', metadata={'failure_reason': 'cancelled'})
        elif result.exit_code == 0 and time.monotonic() >= self.deadline:
            result = Result(1, '', 'invocation deadline exceeded', timed_out=True)
        if reason:
            result = Result(1, '', reason, metadata={'failure_reason': reason})
        metadata = deepcopy(self.last.metadata if self.last is not None else {})
        for key, value in result.metadata.items():
            if key not in metadata or key == 'failure_reason':
                metadata[key] = deepcopy(value)
        result = replace(result, stdout=result.stdout if result.exit_code == 0 else '', metadata=metadata,
                         token_usage=self.tokens if self.complete and self.records else None, cost_usd=self.cost)
        result.metadata['invocation_budgets_v1'] = {'provider_requests': self.requests,
            'tool_calls': self.tools, 'reported_tokens': self.tokens, 'usage_complete': self.complete}
        result.metadata['runtime_adapter_v1'] = {**descriptor, 'attempts': deepcopy(self.records)}
        return result


@dataclass(frozen=True)
class RuntimeAdapter:
    """Versioned trusted invocation hook; execute accepts only reduced authority.

    invoke(artifact, arguments, *, execute, policy, on_event, cancelled, **options)
    must return Result. execute(arguments, **reductions) performs a real Runtime
    invocation. Hooks are cooperative trusted host code, not a sandbox.
    """
    runtime: ProsaicRuntime
    version: str
    invoke: object
    max_attempts: int = 3

    def __post_init__(self):
        if type(self.version) is not str or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', self.version):
            raise ValueError('adapter needs a bounded version')
        if not callable(self.invoke) or type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 8:
            raise ValueError('adapter needs a callback and bounded attempts')
        self.validate_binding()

    def validate_binding(self):
        if (type(self.runtime) is not ProsaicRuntime or
                getattr(self.runtime.run, '__func__', None) is not ProsaicRuntime.run):
            raise ValueError('adapter requires an unmodified concrete ProsaicRuntime')

    @property
    def descriptor(self):
        self.validate_binding()
        return {'version': self.version, 'contract': 'runtime_adapter_v1', 'max_attempts': self.max_attempts}

    @property
    def config(self):
        return self.runtime.config

    @property
    def lifecycle(self):
        return self.runtime.lifecycle

    @property
    def capabilities(self):
        return self.runtime.capabilities | frozenset({'runtime_adapter_v1'})

    @property
    def tool_descriptors(self):
        return self.runtime.tool_descriptors

    @property
    def cli_tool_names(self):
        return self.runtime.cli_tool_names

    @tracked
    def run(self, artifact, arguments='', *, policy=None, on_event=None, cancelled=None, **options):
        self.validate_binding()
        policy = policy or RunPolicy()
        cancelled = cancelled or (lambda: False)
        ledger = _Attempts(policy, self.max_attempts, time.monotonic() + policy.timeout_s, cancelled)
        descriptor = self.descriptor
        def execute(projected, **reductions):
            self.validate_binding()
            effective = ledger.remaining(**reductions)
            if type(projected) is not str:
                raise ValueError('adapter arguments must be text')
            result = self.runtime.run(artifact, projected, policy=effective, on_event=on_event,
                                      cancelled=cancelled, **options)
            ledger.add(result, projected)
            return result
        try:
            result = self.invoke(artifact, arguments, execute=execute, policy=policy,
                                 on_event=on_event, cancelled=cancelled, **options)
        except (InvocationBudgetExceeded, AdapterBoundary) as exc:
            result = Result(130 if exc.reason == 'adapter_cancelled' else 1, '', exc.reason,
                timed_out=exc.reason == 'adapter_timeout', metadata={'failure_reason': exc.reason})
        except Exception:
            result = Result(1, '', 'trusted adapter callback failed', metadata={'failure_reason': 'adapter_callback_failed'})
        return ledger.finish(result, descriptor)
