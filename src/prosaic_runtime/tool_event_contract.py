"""Owned, invocation-local lifecycle projections for explicitly enabled calls."""
from contextvars import ContextVar
from contextlib import contextmanager
from functools import wraps
import hashlib
import json
import time
import unicodedata

from .events import emit, emit_tool_completion
from .tool_contracts import ToolContractBindingsV1, ToolDisplayUseV1, owned
from .tool_result_contract import select_pointer
from .tool_outcomes import ToolOutcome

_bindings = ContextVar('prosaic_tool_contract_bindings', default=None)
_registry = ContextVar('prosaic_tool_evidence_registry', default=None)


def current_bindings():
    return _bindings.get()


def current_registry():
    return _registry.get()


@contextmanager
def tool_contract_session(bindings, registry):
    token, registry_token = _bindings.set(bindings), _registry.set(registry)
    try:
        yield
    finally:
        _registry.reset(registry_token)
        _bindings.reset(token)


def tool_contract_preparation(function):
    """Preparation binds descriptors only and cannot authorize a model delivery."""
    @wraps(function)
    def prepare(*args, **kwargs):
        bindings = kwargs.get('tool_contract_bindings')
        if bindings is not None:
            if type(bindings) is not ToolContractBindingsV1:
                raise ValueError('invalid tool contract bindings')
            bindings.__post_init__()
        token = _bindings.set(bindings)
        try:
            return function(*args, **kwargs)
        finally:
            _bindings.reset(token)
    return prepare


def tool_contract_entry(function):
    """Reset on every public entry, including nested calls without a grant."""
    @wraps(function)
    def invoke(*args, **kwargs):
        from .tool_parallel import assert_not_parallel_worker
        assert_not_parallel_worker()
        policy=kwargs.get('policy')
        if function.__name__=='run_tool' and getattr(policy,'parallel_tools',None) is not None:
            raise ValueError('unsupported direct parallel tools')
        bindings = kwargs.get('tool_contract_bindings')
        if bindings is not None:
            if type(bindings) is not ToolContractBindingsV1:
                raise ValueError('invalid tool contract bindings')
            bindings.__post_init__()
            scope = kwargs.get('operation_context')
            if scope is not None and scope != bindings.scope:
                raise ValueError('tool contract scope mismatch')
            kwargs['operation_context'] = bindings.scope
            if function.__name__ == 'run_tool' and bindings.citations is not None:
                raise ValueError('unsupported direct automatic citations')
        from .tool_evidence import EvidenceRegistryV1
        # Pure registry admission can fail; do not change ambient authority yet.
        registry = EvidenceRegistryV1(bindings) if bindings is not None else None
        token = _bindings.set(bindings)
        registry_token = _registry.set(registry)
        try:
            result = function(*args, **kwargs)
            if registry is not None:
                target = result.result if hasattr(result, 'result') else result
                if not hasattr(result, 'evidence'):
                    target.metadata['tool_contract_v1'] = registry.to_dict()
                if registry.citations is not None:
                    target.metadata['citations_v1'] = registry.citations.to_dict()
            return result
        finally:
            _registry.reset(registry_token)
            _bindings.reset(token)
    return invoke


def call_identity(scope, exchange, index):
    return hashlib.sha256(f'{scope.invocation_id}:{exchange}:{index}'.encode()).hexdigest()


def managed_events(tool, bindings=None):
    return tool is not None and tool.event_policy is not None and (bindings or current_bindings()) is not None


def project_tool_event(tool, bindings, *, call_id, call_index, phase, arguments=None, result=None):
    """Selectors are declarative; authorizers receive identity, never raw payloads."""
    event = dict(version=1, callIndex=call_index, phase=phase, provisional=True)
    policy, grant, limits = tool.event_policy, bindings.display_authorizer, bindings.limits
    if phase in {'rejected', 'uncertain'} or policy is None or grant is None:
        return event
    try:
        use = ToolDisplayUseV1(bindings.scope, tool.name, tool.version, call_id, phase, tuple(policy.fields))
        if grant.authorize(use) is not True:
            return event
    except Exception:
        return event
    display = {}
    unavailable = False
    for name, field in policy.fields.items():
        data = arguments if field.source == 'arguments' else result
        if data is None:
            continue
        try:
            value = select_pointer(data, field.pointer)
        except ValueError:
            continue
        if type(value) not in (str, int, float, bool, type(None)):
            continue
        if type(value) is str:
            if any(unicodedata.category(c) in {'Cc', 'Cf', 'Cs'} for c in value):
                continue
            if len(value.encode('utf-8')) > limits.max_display_string_bytes:
                unavailable = True
                break
        display[name] = value
    if (len(display) > limits.max_display_fields or
            len(json.dumps(display, ensure_ascii=False, allow_nan=False).encode()) > limits.max_display_bytes):
        unavailable = True
    if unavailable:
        event['displayUnavailable'] = True
    elif display:
        event['display'] = display
    if len(json.dumps(event, ensure_ascii=False).encode()) > limits.max_event_bytes:
        event.pop('display', None)
        event['displayUnavailable'] = True
    return owned(event)


class ToolEventLifecycle:
    def __init__(self, tool, bindings, call_index, *, call_id=None, exchange=0):
        self.tool, self.bindings, self.index, self.exchange = tool, bindings, call_index, exchange
        self.call_id = call_id or call_identity(bindings.scope, exchange, call_index)
        self.arguments = None
        self.started_at = None
        self.finished = False
        self.sink_failed = False
        self.outcome = ToolOutcome('unclassified')

    def send(self, phase, result=None):
        fields = dict(name=self.tool.name, tool_version=self.tool.version, call_id=self.call_id, turn=self.exchange,
            toolEventV1=project_tool_event(self.tool, self.bindings, call_id=self.call_id, call_index=self.index,
                phase=phase, arguments=self.arguments, result=result))
        try:
            if phase == 'started':
                emit('tool_started', **fields)
            else:
                emit_tool_completion(**fields, status='ok' if self.outcome.code in {'ok', 'controlled_failure'} and
                    self.outcome.code == 'ok' else 'error', tool_outcome=self.outcome.to_dict(),
                    duration_ms=0 if self.started_at is None else round((time.monotonic() - self.started_at) * 1000, 3))
        except BaseException:
            self.sink_failed = True
            raise

    def start(self, arguments):
        self.arguments = owned(arguments)
        self.started_at = time.monotonic()
        self.send('started')

    def finish(self, outcome=None, status=None):
        if self.finished or self.sink_failed:
            return
        self.finished = True
        if outcome is not None:
            self.outcome = ToolOutcome('ok' if outcome.get('status') == 'ok' else
                'controlled_failure' if status in {'completed', 'replayed'} and self.outcome.code == 'unclassified' else self.outcome.code,
                status == 'replayed' or self.outcome.replayed)
        phase = ('replayed' if self.outcome.replayed else 'uncertain' if status == 'uncertain' or
                 self.started_at is not None and self.outcome.to_dict()['effect'] == 'unknown' else
                 'completed' if self.started_at is not None else 'rejected')
        data = None
        if outcome is not None and outcome.get('status') == 'ok':
            data = outcome['result']['data'] if self.tool.result_contract is not None else outcome['result']
        self.send(phase, data)


class ManagedToolCallV1:
    """Owner-thread lifecycle/capture shared by sequential and parallel dispatch."""
    def __init__(self,tool,raw_arguments,options,*,bindings=None,call_id=None,exchange=0,defer_event=False,parallel=False):
        self.tool,self.raw,self.options=tool,raw_arguments,dict(options)
        self._original=dict(options)
        self.bindings=bindings or current_bindings();self.registry=current_registry()
        self.manage=managed_events(tool,self.bindings)
        self.capture=self.registry is not None and tool.result_contract is not None
        self.active=self.manage or self.capture
        self.defer_event,self.parallel=defer_event,parallel
        self.known=[None,None];self.queued=False;self.lifecycle=None
        self.call_id=call_id
        if not self.active:return
        self.bindings.__post_init__()
        if options.get('scope')!=self.bindings.scope:raise ValueError('tool contract scope mismatch')
        self.call_id=call_id or call_identity(self.bindings.scope,exchange,options.get('call_index',0))
        if self.manage:self.lifecycle=ToolEventLifecycle(tool,self.bindings,options.get('call_index',0),call_id=self.call_id,exchange=exchange)
        self.options.update(on_outcome=self.report,on_completion=self.complete,
            _before_handler=self.lifecycle.start if self.lifecycle is not None else options.get('_before_handler'))

    def report(self,outcome):
        if self.lifecycle is not None:self.lifecycle.outcome=outcome
        callback=self.original_options.get('on_outcome')
        if callback is not None:callback(outcome)

    @property
    def original_options(self):return self._original

    def complete(self,outcome,status='completed',**refs):
        self.known[:]=outcome,status
        callback=self.original_options.get('on_completion')
        if callback is not None:callback(outcome,status,**refs)
        if self.capture:
            from .tools import parse_arguments
            from .tool_effects import canonical_json
            arguments=parse_arguments(self.raw,self.tool.parameters,self.tool.max_argument_bytes)
            self.registry.capture_completion(self.tool,outcome,call_id=self.call_id,
                call_index=self.original_options.get('call_index',0),
                arguments_sha256=hashlib.sha256(canonical_json(arguments).encode()).hexdigest(),status=status)
        if self.lifecycle is not None and not self.defer_event:self.lifecycle.finish(outcome,status)

    def finish(self,result):
        if self.lifecycle is not None:
            if self.defer_event and self.registry is not None:
                self.registry._pending_events[self.call_id]=self.lifecycle;self.queued=True
            else:self.lifecycle.finish(result,self.known[1])

    def close(self):
        if self.lifecycle is not None and not self.queued:
            self.lifecycle.finish(None if self.parallel else self.known[0],
                self.known[1] or ('uncertain' if self.lifecycle.started_at is not None else None))
