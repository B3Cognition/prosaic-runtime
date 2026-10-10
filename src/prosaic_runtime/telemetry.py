"""Synchronous, bounded, best-effort observation; never execution evidence."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import fields
import json
import math
import re

from .operation_context import InvocationScope, bounded_reference
from .policy import BUILTIN_TOOLS

_EVENTS = {
    'runtime': frozenset({'invocation_started', 'invocation_completed', 'provider_request_started',
                         'provider_request_completed', 'tool_started', 'tool_completed'}),
    'harness': frozenset({'run_started', 'transition_committed', 'waiting_committed',
                         'recovery_committed', 'blocked_committed', 'run_completed'}),
}
_OUTCOMES = {
    'runtime': frozenset({'completed', 'cancelled', 'timed_out', 'admission_failure',
                         'critical_hook_error', 'budget_failure', 'execution_failure'}),
    'harness': frozenset({'running', 'waiting', 'blocked', 'completed', 'rejected'}),
}
_REASONS = frozenset({'unknown', 'cancelled', 'invocation_timeout', 'inspection_timeout', 'budget_exceeded',
                      'provider_request_limit', 'tool_call_limit', 'token_limit', 'usage_unknown',
                      'accounting_failed', 'incomplete_response', 'admission_failure',
                      'critical_hook_error', 'execution_failure'})
_COUNTS = frozenset({'calls', 'turn', 'token_usage', 'tool_call_count', 'provider_request_count'})
_DIGESTS = frozenset({'sha256', 'artifact_sha256', 'arguments_sha256'})
_observer = ContextVar('prosaic_observer', default=None)


def _count(value):
    return type(value) is int and 0 <= value <= 2**63 - 1


class ObserverEmitter:
    """Deliver owned safe records. Ordinary callback errors increment failures."""
    def __init__(self, observer, *, source: str, scope: InvocationScope, labels=None):
        if type(source) is not str or source not in _EVENTS:
            raise ValueError('unknown observer source')
        if type(scope) is not InvocationScope:
            raise ValueError('observer scope must be InvocationScope')
        # Revalidate even if a caller has bypassed frozen dataclass assignment.
        InvocationScope.__post_init__(scope)
        if observer is not None and not callable(observer):
            raise ValueError('observer must be callable')
        if labels is not None and (type(labels) is not dict or len(labels) > 16):
            raise ValueError('observer labels must be a dictionary of at most 16 entries')
        labels = {} if labels is None else labels
        if any(not bounded_reference(key, 64) or not bounded_reference(value, 128)
               for key, value in labels.items()):
            raise ValueError('invalid observer label')
        self.observer = observer
        self.source = source
        self._scope = {field.name: getattr(scope, field.name) for field in fields(scope)
                       if getattr(scope, field.name) is not None}
        self._labels = dict(labels)
        self._registered_tools = BUILTIN_TOOLS
        self.sequence = 0
        self.failures = 0
        self.critical_hook_failed = False
        self.execution_started = False

    def emit(self, event: str, **fields) -> None:
        if type(event) is not str or event not in _EVENTS[self.source] or self.observer is None:
            return
        self.sequence += 1
        record = {'version': 1, 'sequence': self.sequence, 'source': self.source,
                  'event': event, **self._scope}
        for key, value in fields.items():
            if key == 'outcome' and type(value) is str and value in _OUTCOMES[self.source]:
                record[key] = value
            elif key == 'reason' and type(value) is str and value in _REASONS:
                record[key] = value
            elif key in _COUNTS and _count(value):
                record[key] = value
            elif key == 'revision' and self.source == 'harness' and (_count(value) or bounded_reference(value, 512)):
                record[key] = value
            elif key in _DIGESTS and type(value) is str and re.fullmatch('[0-9a-f]{64}', value):
                record[key] = value
            elif key == 'name' and self.source == 'runtime':
                record[key] = value if type(value) is str and value in self._registered_tools else 'unknown'
            elif key == 'status' and type(value) is str and value in {'ok', 'error', 'unknown'}:
                record[key] = value
            elif key == 'http_status' and type(value) is int and 100 <= value <= 599:
                record[key] = value
            elif key == 'exit_code' and type(value) is int and 0 <= value <= 255:
                record[key] = value
            elif key == 'duration_ms' and type(value) in (int, float) and 0 <= value <= 2**63 - 1 and math.isfinite(value):
                record[key] = value
        if self._labels:
            record['labels'] = dict(self._labels)
        if len(json.dumps(record).encode('utf-8')) > 4096:
            record.pop('labels', None)
        # All remaining scalars have closed sets or strict byte/numeric limits.
        try:
            self.observer(record)
        except Exception:
            self.failures += 1


@contextmanager
def observation_context(emitter, registered_tools=()):
    emitter._registered_tools = frozenset(BUILTIN_TOOLS) | frozenset(registered_tools)
    token = _observer.set(emitter)
    try:
        yield
    finally:
        _observer.reset(token)


def observe(event, **fields):
    emitter = _observer.get()
    if emitter is not None:
        emitter.emit(event, **fields)


def critical_hook_failed():
    emitter = _observer.get()
    if emitter is not None:
        emitter.critical_hook_failed = True


def execution_started():
    emitter = _observer.get()
    if emitter is not None:
        emitter.execution_started = True
