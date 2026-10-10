"""Host-owned fenced effect journal contract; never a production journal store."""
from dataclasses import dataclass
import inspect
import json
import re
from typing import Protocol

from .operation_context import InvocationScope, bounded_reference


@dataclass(frozen=True)
class ToolClaim:
    state: str
    signature: str
    claim_token: str | None = None
    outcome: dict | None = None


class ToolJournal(Protocol):
    identity: str
    contract_version: str = 'tool-journal-v1'

    def claim(self, operation_namespace: str, operation_key: str, signature: str) -> ToolClaim: ...

    def commit(self, operation_namespace: str, operation_key: str,
               claim_token: str, bounded_outcome: dict) -> None: ...


class ToolEffectFailure(RuntimeError):
    """Critical fixed failure; must never enter model correction or automatic retry."""
    REASONS = frozenset({'tool_effect_uncertain', 'tool_journal_failed', 'tool_identity_conflict'})

    def __init__(self, reason):
        if type(reason) is not str or reason not in self.REASONS:
            raise ValueError('invalid tool effect failure')
        self.reason = reason
        super().__init__(reason)


def valid_identifier(value):
    return bounded_reference(value)


def tool_journal_descriptor(journal):
    """Read declarations without invoking properties, probes, or journal methods."""
    if journal is None:
        return {}
    try:
        version = inspect.getattr_static(journal, 'contract_version')
        identity = inspect.getattr_static(journal, 'identity')
        methods = [inspect.getattr_static(journal, name) for name in ('claim', 'commit')]
    except AttributeError:
        raise ValueError('invalid tool journal declarations') from None
    if type(version) is not str or version != 'tool-journal-v1':
        raise ValueError('invalid tool journal contract')
    if not valid_identifier(identity):
        raise ValueError('invalid tool journal identity')
    for method in methods:
        # A custom descriptor could execute host code at dispatch lookup.
        if not callable(method) or (hasattr(type(method), '__get__') and
                not inspect.isfunction(method) and type(method) is not staticmethod):
            raise ValueError('invalid tool journal methods')
    return {'contract_version': version, 'identity': identity}


def admit_tool_effects(tools, requested, scope, journal):
    tool_journal_descriptor(journal)
    if type(scope) is not InvocationScope:
        raise ValueError('operation_context must be InvocationScope')
    InvocationScope.__post_init__(scope)
    if any(name in tools and tools[name].operation_key is not None for name in requested):
        if scope.operation_namespace is None or journal is None:
            raise ValueError('journaled tools require operation namespace and tool journal')


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def snapshot_outcome(outcome, maximum):
    from .tools import depth
    if type(outcome) is not dict or set(outcome) != {'status', 'result'} or outcome['status'] != 'ok':
        raise ValueError('invalid committed outcome')
    depth(outcome)
    serialized = canonical_json(outcome)
    if len(serialized.encode('utf-8')) > maximum:
        raise ValueError('committed outcome limit')
    return json.loads(serialized)


def validate_claim(claim, signature, maximum):
    if (type(claim) is not ToolClaim or type(claim.signature) is not str or
            re.fullmatch('[0-9a-f]{64}', claim.signature) is None):
        raise ToolEffectFailure('tool_journal_failed')
    if claim.signature != signature:
        raise ToolEffectFailure('tool_identity_conflict')
    if type(claim.state) is not str or claim.state not in {'new', 'replay', 'uncertain'}:
        raise ToolEffectFailure('tool_journal_failed')
    if claim.state == 'new':
        if not bounded_reference(claim.claim_token, 256) or claim.outcome is not None:
            raise ToolEffectFailure('tool_journal_failed')
    elif claim.claim_token is not None:
        raise ToolEffectFailure('tool_journal_failed')
    elif claim.state == 'uncertain':
        if claim.outcome is not None:
            raise ToolEffectFailure('tool_journal_failed')
    else:
        try:
            return snapshot_outcome(claim.outcome, maximum)
        except (TypeError, ValueError, RecursionError, OverflowError, UnicodeError):
            raise ToolEffectFailure('tool_journal_failed') from None
    return None
