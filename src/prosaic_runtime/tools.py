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
from .tool_effects import ToolEffectFailure, canonical_json, snapshot_outcome, validate_claim


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

    def __init__(self, name, description, parameters, handler, version, *,
                 max_argument_bytes=16384, max_result_bytes=65536, authorize=None,
                 with_context=False, operation_key=None):
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
                               with_context=with_context, operation_key=operation_key).items():
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


def bounded_result(value, maximum):
    try:
        depth(value)
        envelope = {'status': 'ok', 'result': value}
        text = json.dumps(envelope, allow_nan=False)
        if len(text.encode('utf-8')) > maximum:
            return {'status': 'error', 'error': 'result_limit'}
        return json.loads(text)
    except (TypeError, ValueError, RecursionError, OverflowError):
        return {'status': 'error', 'error': 'invalid_result'}


def execute_custom_tool(tool, raw_arguments, *, check_boundary, scope=None,
                        deadline=None, cancelled=lambda: False, call_index=0, tool_journal=None):
    check_boundary()
    try:
        arguments = parse_arguments(raw_arguments, tool.parameters, tool.max_argument_bytes)
    except (TypeError, ValueError, RecursionError, OverflowError, ValidationError):
        return {'status': 'error', 'error': 'invalid_arguments'}
    context = None
    if tool.with_context:
        try:
            digest = hashlib.sha256(canonical_json(arguments).encode('utf-8')).hexdigest()
        except (TypeError, ValueError, RecursionError, OverflowError):
            return {'status': 'error', 'error': 'invalid_arguments'}
        context = ToolExecutionContext(scope, tool.name, tool.version, call_index,
            digest, deadline, cancelled)

    def invoke(callback, ctx=context):
        owned = deepcopy(arguments)
        return callback(owned, ctx) if tool.with_context else callback(owned)

    if tool.authorize is not None:
        try:
            allowed = invoke(tool.authorize) is True
        except (Cancelled, ToolDeadlineExceeded):
            raise
        except Exception:
            allowed = False
        check_boundary()
        if not allowed:
            return {'status': 'error', 'error': 'authorization_denied'}
    check_boundary()
    if tool.operation_key is not None:
        try:
            key = tool.operation_key(deepcopy(arguments), context)
        except (Cancelled, ToolDeadlineExceeded):
            raise
        except Exception:
            raise ToolEffectFailure('tool_identity_conflict') from None
        if not bounded_reference(key, 256):
            raise ToolEffectFailure('tool_identity_conflict')
        check_boundary()
        context = replace(context, operation_key=key)
        signature = hashlib.sha256(canonical_json({'tool_name': tool.name,
            'tool_version': tool.version, 'arguments': arguments}).encode('utf-8')).hexdigest()
        try:
            claim = tool_journal.claim(scope.operation_namespace, key, signature)
        except Exception:
            raise ToolEffectFailure('tool_journal_failed') from None
        replay = validate_claim(claim, signature, tool.max_result_bytes)
        if claim.state == 'uncertain':
            raise ToolEffectFailure('tool_effect_uncertain')
        check_boundary()
        if claim.state == 'replay':
            return replay
        try:
            value = invoke(tool.handler, context)
            outcome = snapshot_outcome({'status': 'ok', 'result': value}, tool.max_result_bytes)
        except Exception:
            # Dispatch errors and unusable results cannot establish completion.
            raise ToolEffectFailure('tool_effect_uncertain') from None
        try:
            tool_journal.commit(scope.operation_namespace, key, claim.claim_token, deepcopy(outcome))
        except Exception:
            raise ToolEffectFailure('tool_journal_failed') from None
        # Commit valid completion before observing cancellation or deadline.
        check_boundary()
        return outcome
    try:
        value = invoke(tool.handler)
    except (Cancelled, ToolDeadlineExceeded):
        raise
    except ToolExecutionError as exc:
        check_boundary()
        return {'status': 'error', 'error': exc.code}
    except Exception:
        check_boundary()
        return {'status': 'error', 'error': 'handler_error'}
    check_boundary()
    result = bounded_result(value, tool.max_result_bytes)
    check_boundary()
    return result
