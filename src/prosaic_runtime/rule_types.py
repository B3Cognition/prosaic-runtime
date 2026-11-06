"""Rule admission types: the guard protocol, verdicts, contexts and setting shapes.

See rules.py for compilation and docs/rules-admission.md for the contract.
"""

from collections.abc import Sequence
from dataclasses import dataclass
import json
import re

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

STAGES = ('before_tool', 'after_tool', 'before_answer', 'final')
ACTIONS = ('fix', 'retry', 'fail')
STRENGTH = {'fix': 0, 'retry': 1, 'fail': 2}
NAME = re.compile(r'[a-z][a-z0-9_]{0,63}')
MAX_RULES = 256
MAX_TEXT_BYTES = 16_384
MAX_SETTINGS_BYTES = 65_536


class RuleAdmissionError(ValueError):
    """Closed refusal; never echoes rule text, settings or values."""

    def __init__(self, code='invalidArtifact', detail=None):
        if code not in {'invalidArtifact', 'invalidPolicy'}:
            raise ValueError('invalid rule admission code')
        # detail names host policy only (a guard name and a pinned field), never prose values.
        self.code, self.detail = code, detail
        super().__init__(f'enforcement rule admission refused: {code}' + (f' ({detail})' if detail else ''))


class RuleBindingWarning(UserWarning):
    """The same guard was bound at one stage in several files; the stronger action was kept."""


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str | None = None
    refused_values: tuple = ()
    fixed: object = None

    def __post_init__(self):
        if isinstance(self.refused_values, list):
            object.__setattr__(self, 'refused_values', tuple(self.refused_values))


class ToolResults(Sequence):
    """Read-only tool results the model has seen this turn.

    Each result is encoded once when observed and decoded only when a guard
    reads it, as a copy private to that check, so checks that never read
    results cost nothing and no check re-serializes the turn's history.
    ``builtin[i]`` is True when result ``i`` came from a built-in tool
    (``read_file`` and the other path-scoped tools), whose facts have a fixed
    shape, rather than from a custom or CLI tool, whose shape is arbitrary.
    """

    __slots__ = ('_encoded', '_decoded', '_builtin')
    __hash__ = None

    def __init__(self, encoded=(), builtin=()):
        self._encoded, self._decoded = tuple(encoded), {}
        flags = tuple(bool(flag) for flag in builtin)[: len(self._encoded)]
        self._builtin = flags + (False,) * (len(self._encoded) - len(flags))

    @property
    def builtin(self):
        return self._builtin

    def __len__(self):
        return len(self._encoded)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return tuple(self[i] for i in range(*index.indices(len(self))))
        position = range(len(self))[index]  # IndexError and negative indices as for a tuple.
        if position not in self._decoded:
            self._decoded[position] = json.loads(self._encoded[position])
        return self._decoded[position]

    def __eq__(self, other):
        if isinstance(other, (tuple, list, ToolResults)):
            return tuple(self) == tuple(other)
        return NotImplemented

    def __repr__(self):
        return f'ToolResults({tuple(self)!r})'


class Encoded:
    """JSON text standing in for a fact until a guard reads it."""

    __slots__ = ('text',)

    def __init__(self, text):
        self.text = text


def encode_fact(value):
    """Encode containers once; immutable scalars pass through unchanged."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return Encoded(json.dumps(value, ensure_ascii=False, allow_nan=False))


_LAZY = ('settings', 'paths', 'input_data', 'arguments', 'tool_result')
_FIELDS = ('stage', 'rule_id', 'rule_text', 'source_file', 'user_message', 'tool_results', 'tool_name', 'draft')


class GuardContext:
    """Observed facts for one check, read-only.

    JSON facts (settings, paths, input_data, arguments, tool_result) are
    decoded only when a guard reads them, as copies private to this check; a
    guard that mutates one cannot affect another check. tool_results is a
    read-only sequence of the results the model has seen this turn.
    """

    __slots__ = (*_FIELDS, *(f'_{name}' for name in _LAZY), '_decoded')

    def __init__(
        self,
        stage,
        rule_id,
        rule_text,
        source_file,
        settings,
        paths,
        user_message,
        input_data,
        tool_results,
        tool_name=None,
        arguments=None,
        tool_result=None,
        draft=None,
    ):
        values = dict(
            stage=stage,
            rule_id=rule_id,
            rule_text=rule_text,
            source_file=source_file,
            user_message=user_message,
            tool_results=tool_results,
            tool_name=tool_name,
            draft=draft,
            _settings=settings,
            _paths=paths,
            _input_data=input_data,
            _arguments=arguments,
            _tool_result=tool_result,
            _decoded={},
        )
        for name, value in values.items():
            object.__setattr__(self, name, value)

    def _fact(self, name):
        decoded = object.__getattribute__(self, '_decoded')
        if name not in decoded:
            value = object.__getattribute__(self, f'_{name}')
            decoded[name] = json.loads(value.text) if isinstance(value, Encoded) else value
        return decoded[name]

    settings = property(lambda self: self._fact('settings'))
    paths = property(lambda self: self._fact('paths'))
    input_data = property(lambda self: self._fact('input_data'))
    arguments = property(lambda self: self._fact('arguments'))
    tool_result = property(lambda self: self._fact('tool_result'))

    def __setattr__(self, name, value):
        raise AttributeError('GuardContext is read-only')

    def __repr__(self):
        return f'GuardContext(stage={self.stage!r}, rule_id={self.rule_id!r})'


@dataclass(frozen=True, init=False)
class Guard:
    """A host-registered check. Markdown can bind it, never define or discover it."""

    name: str
    version: str
    stages: tuple
    check: object
    _schema_json: str
    path_settings: tuple
    fixes: bool
    echo_values: bool

    def __init__(
        self, name, version, stages, check, *, fixes=False, echo_values=False, settings_schema=None, path_settings=()
    ):
        from .tools import _reject_refs

        if type(name) is not str or not NAME.fullmatch(name):
            raise ValueError('guard name must match [a-z][a-z0-9_]*')
        if type(version) is not str or not version.strip() or len(version.encode('utf-8')) > 128:
            raise ValueError('invalid guard version')
        if (
            type(stages) not in (tuple, list)
            or not stages
            or len(set(stages)) != len(stages)
            or any(stage not in STAGES for stage in stages)
        ):
            raise ValueError('guard stages must be distinct accepted stages')
        if not callable(check):
            raise ValueError('guard check must be callable')
        if type(fixes) is not bool:
            raise ValueError('guard fixes must be boolean')
        # Answer-stage feedback goes to the model in the user role. Raw refused values may echo
        # tool or web text from the draft, so a guard opts in only when its values cannot carry
        # instructions (for example, regex-constrained figures). Tool-stage feedback is tool-role.
        if type(echo_values) is not bool:
            raise ValueError('guard echo_values must be boolean')
        schema = {'type': 'object', 'additionalProperties': False} if settings_schema is None else settings_schema
        try:
            if type(schema) is not dict or schema.get('type') != 'object' or 'additionalProperties' not in schema:
                raise ValueError('settings schema must be an object schema with explicit additionalProperties')
            _reject_refs(schema)
            schema_json = json.dumps(schema, sort_keys=True, allow_nan=False)
            Draft202012Validator.check_schema(json.loads(schema_json))
        except (TypeError, SchemaError, RecursionError, OverflowError) as exc:
            raise ValueError('invalid guard settings schema') from exc
        if type(path_settings) not in (tuple, list) or any(
            type(key) is not str or key not in schema.get('properties', {}) for key in path_settings
        ):
            raise ValueError('path settings must name declared settings')
        for key, value in dict(
            name=name,
            version=version,
            stages=tuple(stages),
            check=check,
            _schema_json=schema_json,
            path_settings=tuple(path_settings),
            fixes=fixes,
            echo_values=echo_values,
        ).items():
            object.__setattr__(self, key, value)

    @property
    def settings_schema(self):
        return json.loads(self._schema_json)


def validate_guards(guards):
    if guards is None:
        return {}
    registry = {}
    for guard in guards:
        if type(guard) is not Guard:
            raise ValueError('guards must be Guard registrations')
        if guard.name in registry:
            raise ValueError('duplicate guard registration')
        registry[guard.name] = guard
    return registry


def _scalar(value):
    return type(value) is int or (type(value) is str and len(value.encode('utf-8')) <= MAX_SETTINGS_BYTES)


def settings_ok(settings):
    """Tag setting values exactly as Core's tokenizer produces them."""
    if type(settings) is not dict:
        return False
    for key, setting in settings.items():
        if type(key) is not str or not NAME.fullmatch(key):
            return False
        if not (_scalar(setting) or (type(setting) is list and all(_scalar(item) for item in setting))):
            return False
    return True


def validate_settings(guard, settings):
    validator = Draft202012Validator(guard.settings_schema)
    if next(iter(validator.iter_errors(settings)), None) is not None:
        raise RuleAdmissionError()
