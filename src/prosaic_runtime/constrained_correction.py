"""Constrain the one tool-request correction to a call Runtime can accept.

With ``features.constrained_tool_correction`` on a trusted, nonstreaming OpenAI-compatible profile and
``correct_malformed_tool_calls=True``, the correction request for a refused singleton call carries a JSON
schema for the resend, ``{"tool": ..., "arguments": ...}``:

* ``argumentsSchema`` on an identifiable tool: ``tool`` is that name and ``arguments`` its schema.
* ``toolNotGranted``: one ``anyOf`` branch per offered (granted) tool, linking each name to its arguments,
  or, when the branches exceed the bounds, the names alone as an ``enum``.

The value selects the wire: ``json_schema`` sends ``response_format`` (strict ``json_schema``) and
``omlx_grammar`` sends oMLX's ``structured_outputs.json``; both set ``tool_choice`` to ``"none"`` so the
reply is the constrained content. The wire schema keeps only the structural keywords in ``KEYWORDS``;
assertion keywords such as ``maximum`` or ``pattern`` are dropped and named in the evidence, and batch
admission still checks the tool's real schema. A schema outside the bounds, another issue or a multi-call
batch keeps ordinary correction and records why. Content that fits becomes one host-made native call
which then passes every argument, authorisation and budget check; anything else ends the turn with the
existing ``tool_choice_not_honored``. The provider is never trusted to have applied the constraint.
"""

from dataclasses import dataclass
import hashlib
import json

from jsonschema import Draft202012Validator

FEATURE = 'constrained_tool_correction'
WIRES = ('json_schema', 'omlx_grammar')
KEYWORDS = frozenset({'type', 'properties', 'required', 'additionalProperties', 'items', 'enum', 'const', 'anyOf'})
# The existing tool-argument and MCP schema byte limit, the JSON depth limit and the turn node limit.
MAX_SCHEMA_BYTES = 16_384
MAX_SCHEMA_DEPTH = 64
MAX_SCHEMA_NODES = 65_536
RECORD_KEY = 'tool_request_correction_constraint'
# The evidence lists at most this many dropped keyword names, in at most this many JSON bytes.
MAX_DROPPED_NAMES = 32
MAX_DROPPED_BYTES = 1024
# Named in the existing tool_choice_not_honored failure when no single tool name was required.
EXPECTED_TOOL = 'constrained tool request'


@dataclass(frozen=True)
class Plan:
    """The derived wire schema (None on fallback) and the evidence record for this correction."""

    schema: dict | None
    record: dict


def validate_profile(endpoint):
    """Refuse values and combinations outside the supported configuration before any request."""
    features = endpoint.features
    if FEATURE not in features:
        return
    wire = features[FEATURE]
    if type(wire) is not str or wire not in WIRES:
        raise ValueError(f'{FEATURE} must be one of {", ".join(WIRES)}')
    if (
        endpoint.provider != 'openai-compatible'
        or features.get('streaming') is not False
        or features.get('omlx_tool_call_envelope', False)
        or features.get('omlx_nemotron_tool_correction', False)
    ):
        raise ValueError(
            f'{FEATURE} requires a nonstreaming OpenAI-compatible profile without '
            'omlx_tool_call_envelope or omlx_nemotron_tool_correction'
        )


def plan(features, error, *, batch_size, tools, required_tool=None):
    """Derive the constraint for ``error``, or return None when the opt-in is absent."""
    wire = features.get(FEATURE)
    if wire not in WIRES:
        return None
    base = {'version': 1, 'wire': wire, 'issue': error.issue}
    offered = {tool['function']['name']: tool['function']['parameters'] for tool in tools}
    if required_tool is not None:
        offered = {name: schema for name, schema in offered.items() if name == required_tool}
    if batch_size != 1:
        return Plan(None, {**base, 'fallback': 'batch'})
    if error.issue == 'argumentsSchema' and type(error.tool) is str and error.tool in offered:
        offered = {error.tool: offered[error.tool]}
    elif error.issue != 'toolNotGranted' or not offered:
        return Plan(None, {**base, 'fallback': 'issue'})
    dropped, scope = set(), 'arguments'
    schema, reason = _derive(offered, dropped)
    if schema is None and len(offered) > 1:
        dropped, scope = set(), 'name'
        schema, reason = _checked(_branch({'enum': list(offered)}, {'type': 'object'}))
    if schema is None:
        return Plan(None, {**base, 'fallback': reason})
    record = {**base, 'scope': scope, 'schemaSha256': hashlib.sha256(_wire_bytes(schema)).hexdigest()}
    return Plan(schema, {**record, **_dropped_names(dropped)})


def apply(payload, constraint, features):
    """Put the constraint on this one correction request; the next request is rebuilt without it."""
    payload['tool_choice'] = 'none'
    if features[FEATURE] == 'json_schema':
        payload.pop('structured_outputs', None)
        payload['response_format'] = {
            'type': 'json_schema',
            'json_schema': {'name': 'tool_request', 'schema': constraint.schema, 'strict': True},
        }
    else:
        payload.pop('response_format', None)
        payload['structured_outputs'] = {'json': constraint.schema}


def accept(turn, constraint, messages):
    """Turn conforming content into one host-made call; return False for anything else."""
    from .tools import depth, reject_constant, unique_pairs

    if turn.tool_calls or turn.finish_reason != 'stop':
        return False
    try:
        value = json.loads(turn.text, object_pairs_hook=unique_pairs, parse_constant=reject_constant)
        depth(value)
    except (ValueError, TypeError, RecursionError):
        return False
    if type(value) is not dict or not Draft202012Validator(constraint.schema).is_valid(value):
        return False
    used = {
        call.get('id')
        for message in messages
        if type(message.get('tool_calls')) is list
        for call in message['tool_calls']
        if type(call) is dict
    }
    index = 1
    while f'correction-{index}' in used:
        index += 1
    turn.tool_calls = [
        {
            'id': f'correction-{index}',
            'type': 'function',
            'function': {'name': value['tool'], 'arguments': json.dumps(value['arguments'], ensure_ascii=False)},
        }
    ]
    turn.text, turn.finish_reason = '', 'tool_calls'
    return True


def _branch(tool, arguments):
    return {
        'type': 'object',
        'properties': {'tool': tool, 'arguments': arguments},
        'required': ['tool', 'arguments'],
        'additionalProperties': False,
    }


class _Unsupported(ValueError):
    """A schema shape the wire constraint cannot carry faithfully."""


def _derive(offered, dropped):
    """The per-tool constraint, or (None, reason)."""
    from .tools import depth

    branches = []
    for name, parameters in offered.items():
        if type(parameters) is not dict:
            return None, 'unsupported'  # Arguments are always a JSON object.
        try:
            depth(parameters)  # Iterative: bounds nesting before any recursion below.
        except ValueError as exc:
            return None, 'oversize' if 'nesting' in str(exc) else 'unsupported'
        try:
            branches.append(_branch({'const': name}, _structural(parameters, dropped)))
        except _Unsupported:
            return None, 'unsupported'
    return _checked(branches[0] if len(branches) == 1 else {'anyOf': branches})


def _checked(schema):
    """``schema`` if it is a valid schema within the bounds, else (None, reason)."""
    from jsonschema.exceptions import SchemaError

    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        return None, 'unsupported'
    return (schema, None) if _bounded(schema) is not None else (None, 'oversize')


def _wire_bytes(schema):
    """The schema's bytes as Runtime's request encoder, ``json.dumps(payload, allow_nan=False)``, sends them."""
    return json.dumps(schema, allow_nan=False).encode()


def _dropped_names(dropped):
    """Sorted dropped keyword names, bounded so the evidence never crowds out other result metadata."""
    names, kept = sorted(dropped), []
    for name in names:
        if len(kept) == MAX_DROPPED_NAMES or len(json.dumps([*kept, name])) > MAX_DROPPED_BYTES:
            break
        kept.append(name)
    omitted = len(names) - len(kept)
    return {'droppedKeywords': kept, **({'droppedKeywordsOmitted': omitted} if omitted else {})}


def _structural(schema, dropped):
    """Copy ``schema`` keeping only ``KEYWORDS``; collect the names of the keywords left out."""
    if type(schema) is bool:
        return schema
    if type(schema) is not dict:
        raise _Unsupported('subschema')
    kept = {}
    for key, value in schema.items():
        if key not in KEYWORDS:
            dropped.add(key)
        elif key == 'properties':
            if type(value) is not dict:
                raise _Unsupported(key)
            kept[key] = {name: _structural(child, dropped) for name, child in value.items()}
        elif key in ('items', 'additionalProperties'):
            kept[key] = _structural(value, dropped)  # A list (the tuple form of items) is unsupported.
        elif key == 'anyOf':
            if type(value) is not list or not value:
                raise _Unsupported(key)
            kept[key] = [_structural(child, dropped) for child in value]
        elif key == 'required' and (type(value) is not list or any(type(v) is not str for v in value)):
            raise _Unsupported(key)
        elif key == 'enum' and type(value) is not list:
            raise _Unsupported(key)
        elif key == 'type' and not (type(value) is str or type(value) is list and all(type(v) is str for v in value)):
            raise _Unsupported(key)
        else:
            kept[key] = value  # type, required, enum and const values are data.
    return kept


def _bounded(schema):
    """Canonical bytes within Runtime's schema bounds, or None."""
    from .conversation import encode_owned

    try:
        return encode_owned(schema, max_bytes=MAX_SCHEMA_BYTES, max_depth=MAX_SCHEMA_DEPTH, max_nodes=MAX_SCHEMA_NODES)
    except ValueError:
        return None
