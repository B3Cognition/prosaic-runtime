"""Validated host tools. Callbacks are trusted code, not sandboxed or preemptible."""
from collections.abc import Mapping, Callable
from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import json
import math
import re
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError, SchemaError
from .events import Cancelled
from .policy import BUILTIN_TOOLS
from .operation_context import ToolExecutionContext, bounded_reference
from .tool_effects import ToolEffectFailure, ToolFailure, canonical_json, snapshot_outcome, validate_claim, failure_outcome
from .tool_outcomes import ToolOutcome
from .terminal import TerminalCompletion, TerminalCompletionError
from .tool_contracts import ToolResultContractV1, ToolEventPolicyV1


class ToolDeadlineExceeded(TimeoutError):
    """The shared invocation deadline expired at a callback boundary."""


class ToolExecutionError(RuntimeError):
    """A safe machine code, never raw subprocess output or exception text."""
    def __init__(self, code):
        if code not in {'cli_unavailable', 'cli_environment', 'cli_timeout', 'cli_output_limit',
                        'cli_path_denied', 'cli_context', 'cli_arguments', 'cli_exit',
                        'cli_invalid_output', 'cli_version', 'cli_sandbox_unavailable'}:
            raise ValueError('unknown tool error code')
        self.code = code
        super().__init__(code)


def depth(value):
    """Validate finite JSON data, bounding traversal before serialization."""
    stack = [(value, 0, frozenset())]
    maximum = 0
    while stack:
        child, level, ancestors = stack.pop()
        if type(child) in (dict, list):
            level += 1
            if level > 64 or id(child) in ancestors:
                raise ValueError('JSON nesting or cycle')
            maximum = max(maximum, level)
            if type(child) is dict and any(type(k) is not str for k in child):
                raise ValueError('JSON keys must be strings')
            ancestors = ancestors | {id(child)}
            stack.extend((v, level, ancestors) for v in
                         (child.values() if type(child) is dict else child))
        elif child is not None and type(child) not in (str, bool, int, float):
            raise ValueError('not JSON data')
        elif type(child) is float and not math.isfinite(child):
            raise ValueError('nonfinite number')
    return maximum


def _reject_refs(value):
    if isinstance(value, dict):
        if {'$ref', '$dynamicRef'} & value.keys():
            raise ValueError('tool schemas cannot use references')
        for child in value.values():
            _reject_refs(child)
    elif isinstance(value, list):
        for child in value:
            _reject_refs(child)


@dataclass(frozen=True, init=False)
class CustomTool:
    """Immutable definition; version must change when handler semantics/data change."""
    name: str
    description: str
    _schema_json: str
    handler: Callable
    version: str
    max_argument_bytes: int
    max_result_bytes: int
    authorize: Callable | None
    with_context: bool
    operation_key: Callable | None
    terminal: bool
    result_contract: ToolResultContractV1 | None
    event_policy: ToolEventPolicyV1 | None
    parallel_read: bool

    def __init__(self, name, description, parameters, handler, version, *,
                 max_argument_bytes=16384, max_result_bytes=65536, authorize=None,
                 with_context=False, operation_key=None, terminal=False,
                 result_contract=None, event_policy=None, parallel_read=False):
        if type(parallel_read) is not bool or parallel_read and (not with_context or terminal):
            raise ValueError('parallel reads require contextual nonterminal tools')
        if result_contract is not None and type(result_contract) is not ToolResultContractV1:
            raise ValueError('invalid tool result contract')
        if event_policy is not None and type(event_policy) is not ToolEventPolicyV1:
            raise ValueError('invalid tool event policy')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', name) or name in BUILTIN_TOOLS:
            raise ValueError('invalid or reserved custom tool name')
        for label, value, limit in [('description', description, 4096), ('version', version, 128)]:
            if not isinstance(value, str) or not value.strip() or len(value.encode('utf-8')) > limit:
                raise ValueError(f'invalid tool {label}')
        for value in (max_argument_bytes, max_result_bytes):
            if type(value) is not int or value <= 0:
                raise ValueError('tool byte limits must be positive integers')
        if not callable(handler) or (authorize is not None and not callable(authorize)):
            raise ValueError('handler and authorizer must be callable')
        if type(with_context) is not bool:
            raise ValueError('with_context must be boolean')
        if type(terminal) is not bool:
            raise ValueError('terminal must be boolean')
        if terminal and not bounded_reference(version):
            raise ValueError('invalid terminal tool version')
        if operation_key is not None and (not with_context or not callable(operation_key)):
            raise ValueError('operation_key requires contextual tools and a callable resolver')
        try:
            depth(parameters)
            if type(parameters) is not dict or parameters.get('type') != 'object' or parameters.get('additionalProperties') is not False:
                raise ValueError('tool parameters must be a closed object schema')
            _reject_refs(parameters)
            schema_json = json.dumps(parameters, sort_keys=True, allow_nan=False)
            Draft202012Validator.check_schema(json.loads(schema_json))
        except (TypeError, SchemaError, RecursionError, OverflowError) as exc:
            raise ValueError('invalid tool schema') from exc
        for key, value in dict(name=name, description=description, _schema_json=schema_json,
                               handler=handler, version=version, max_argument_bytes=max_argument_bytes,
                               max_result_bytes=max_result_bytes, authorize=authorize,
                               with_context=with_context, operation_key=operation_key, terminal=terminal,
                               result_contract=result_contract, event_policy=event_policy, parallel_read=parallel_read).items():
            object.__setattr__(self, key, value)

    @property
    def parameters(self):
        return json.loads(self._schema_json)

    @property
    def descriptor(self):
        descriptor = dict(name=self.name, description=self.description, parameters=self.parameters,
                    version=self.version, max_argument_bytes=self.max_argument_bytes,
                    max_result_bytes=self.max_result_bytes, authorization_required=self.authorize is not None)
        if self.with_context:
            descriptor['with_context'] = True
        if self.operation_key is not None:
            descriptor['journaled'] = True
        if self.terminal:
            descriptor['terminal'] = True
        if self.parallel_read:
            descriptor['parallel_read'] = True
        if self.result_contract is not None:
            descriptor['result_contract'] = self.result_contract.to_dict()
        if self.event_policy is not None:
            descriptor['event_policy'] = self.event_policy.to_dict()
        return descriptor


def validate_custom_tools(tools):
    if tools is None:
        return {}
    if not isinstance(tools, Mapping):
        raise ValueError('custom_tools must be a mapping')
    snapshot = {}
    for key, tool in tools.items():
        if type(tool) is not CustomTool or key != tool.name or key in snapshot:
            raise ValueError('custom tool registry key mismatch or invalid definition')
        snapshot[key] = tool
    return snapshot


def custom_descriptors(tools):
    return {name: tool.descriptor for name, tool in tools.items()}


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError('nonfinite number')


def parse_arguments(raw, schema, maximum):
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > maximum:
        raise ValueError('argument limit')
    arguments = json.loads(raw, object_pairs_hook=unique_pairs, parse_constant=reject_constant)
    if type(arguments) is not dict:
        raise ValueError('arguments must be an object')
    depth(arguments)
    Draft202012Validator(schema).validate(arguments)
    return arguments


def bounded_result(value, maximum, *, on_outcome=None):
    try:
        envelope = failure_outcome(value)
        depth(envelope['result'] if envelope['status']=='ok' else envelope)
        text = json.dumps(envelope, allow_nan=False)
        if len(text.encode('utf-8')) > maximum:
            result, code = {'status': 'error', 'error': 'result_limit'}, 'result_limit'
        else:
            result, code = json.loads(text), 'controlled_failure' if type(value) is ToolFailure else 'ok'
    except (TypeError, ValueError, RecursionError, OverflowError):
        result, code = {'status': 'error', 'error': 'invalid_result'}, 'invalid_result'
    if on_outcome is not None:
        on_outcome(ToolOutcome(code))
    return result


def execute_custom_tool(tool, raw_arguments, *, check_boundary, scope=None,
                        deadline=None, cancelled=lambda: False, call_index=0, tool_journal=None,
                        on_completion=None, accounting_context=None, on_outcome=None,
                        on_terminal=None, tool_contract_bindings=None, _event_call_id=None, _event_exchange=0,
                        _defer_event=False, _parallel_mode=False, _phase_state=None):
    from .tool_parallel import assert_not_parallel_worker
    from .tool_event_contract import ManagedToolCallV1
    assert_not_parallel_worker()
    options=dict(check_boundary=check_boundary,scope=scope,deadline=deadline,cancelled=cancelled,
        call_index=call_index,tool_journal=tool_journal,on_completion=on_completion,
        accounting_context=accounting_context,on_outcome=on_outcome,on_terminal=on_terminal,
        _parallel_mode=_parallel_mode,_phase_state=_phase_state if _phase_state is not None else {})
    managed=ManagedToolCallV1(tool,raw_arguments,options,bindings=tool_contract_bindings,
        call_id=_event_call_id,exchange=_event_exchange,defer_event=_defer_event,parallel=_parallel_mode)
    try:
        result=_execute_custom_tool(tool,raw_arguments,**managed.options)
        managed.finish(result)
        return result
    finally:managed.close()


def _execute_custom_tool(tool, raw_arguments, *, check_boundary, scope=None,
                        deadline=None, cancelled=lambda: False, call_index=0, tool_journal=None,
                        on_completion=None, accounting_context=None, on_outcome=None,
                        on_terminal=None, _before_handler=None, _parallel_mode=False, _phase_state=None):
    from .tool_dispatch import (prepare_custom_tool_call, PreparedToolCallV1,
        start_prepared_tool, invoke_prepared_tool, settle_custom_tool_call)
    call = prepare_custom_tool_call(tool, raw_arguments, call_id=str(call_index), call_index=call_index,
        batch_index=call_index, options=dict(check_boundary=check_boundary, scope=scope, deadline=deadline,
        cancelled=cancelled, tool_journal=tool_journal, on_completion=on_completion,
        accounting_context=accounting_context, on_outcome=on_outcome, on_terminal=on_terminal,
        _before_handler=_before_handler, _parallel_mode=_parallel_mode,
        _phase_state=_phase_state if _phase_state is not None else {}))
    if not isinstance(call, PreparedToolCallV1):
        return call.outcome
    try:start_prepared_tool(call)
    except BaseException as exc:
        if _parallel_mode and call.operation_key is not None:
            from .tool_dispatch import WorkerSettlementV1
            settle_custom_tool_call(call,WorkerSettlementV1(call.call_id,call.call_index,call.batch_index,
                'tool_effect_uncertain',exception=exc))
        raise
    call.options['_phase_state']['handlerEntered']=True
    return settle_custom_tool_call(call, invoke_prepared_tool(call)).outcome
