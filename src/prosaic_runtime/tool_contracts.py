"""Owned opt-in tool declarations; JSON content never supplies authority."""
from collections.abc import Mapping
from dataclasses import dataclass, fields, asdict
import json
import re
from types import MappingProxyType

from jsonschema import Draft202012Validator
from .conversation import encode_owned
from .operation_context import InvocationScope, bounded_reference


def owned(value, maximum=4_194_304):
    return json.loads(encode_owned(value, max_bytes=maximum))


def reference(value, maximum=128):
    if not bounded_reference(value, maximum):
        raise ValueError('invalid tool contract reference')
    return value


def text(value, maximum, *, empty=False):
    if type(value) is not str or (not empty and not value) or len(value.encode('utf-8')) > maximum:
        raise ValueError('invalid tool contract text')
    return value


def pointer(value):
    text(value, 4096, empty=True)
    if value and (not value.startswith('/') or re.search(r'~(?![01])', value)):
        raise ValueError('invalid tool contract pointer')
    return value


def schema_snapshot(value):
    if value is None:
        return None
    snapshot = owned(value, 65536)
    if type(snapshot) is not dict:
        raise ValueError('invalid tool result schema')
    stack = [snapshot]
    while stack:
        child = stack.pop()
        if type(child) is dict:
            if {'$ref', '$dynamicRef'} & child.keys():
                raise ValueError('tool schemas cannot use references')
            stack.extend(child.values())
        elif type(child) is list:
            stack.extend(child)
    try:
        Draft202012Validator.check_schema(snapshot)
    except Exception:
        raise ValueError('invalid tool result schema') from None
    return json.dumps(snapshot, sort_keys=True, allow_nan=False)


@dataclass(frozen=True)
class ToolContractLimitsV1:
    max_event_bytes: int = 4096
    max_display_fields: int = 8
    max_display_string_bytes: int = 256
    max_display_bytes: int = 1024
    max_evidence_items: int = 64
    max_registry_items: int = 256
    max_evidence_bytes: int = 65536
    max_citations: int = 128
    max_citation_bytes: int = 32768

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or not 0 < value <= field.default:
                raise ValueError('tool contract limits may only narrow')

    def to_dict(self):
        self.__post_init__()
        return asdict(self)


@dataclass(frozen=True)
class EvidenceDraftV1:
    source_key: str
    source_revision: str | None
    locator: str
    selector: str
    label: str

    def __post_init__(self):
        reference(self.source_key, 256)
        if self.source_revision is not None:
            text(self.source_revision, 128)
        text(self.locator, 1024, empty=True)
        pointer(self.selector)
        text(self.label, 128)

    def to_dict(self):
        self.__post_init__()
        return dict(sourceKey=self.source_key, sourceRevision=self.source_revision,
                    locator=self.locator, selector=self.selector, label=self.label)

    @classmethod
    def from_dict(cls, value):
        if type(value) is not dict or set(value) != {'sourceKey', 'sourceRevision', 'locator', 'selector', 'label'}:
            raise ValueError('invalid evidence declaration')
        return cls(value['sourceKey'], value['sourceRevision'], value['locator'], value['selector'], value['label'])


@dataclass(frozen=True, init=False)
class ToolResultV1:
    _data_json: str
    evidence: tuple

    def __init__(self, *, data, evidence=()):
        if type(evidence) not in (tuple, list) or len(evidence) > 64 or any(type(e) is not EvidenceDraftV1 for e in evidence):
            raise ValueError('invalid tool evidence declarations')
        declarations = tuple(EvidenceDraftV1.from_dict(e.to_dict()) for e in evidence)
        object.__setattr__(self, '_data_json', encode_owned(data).decode('utf-8'))
        object.__setattr__(self, 'evidence', declarations)

    @property
    def data(self):
        return json.loads(self._data_json)

    def to_dict(self):
        return dict(version=1, data=self.data, sources=[e.to_dict() for e in self.evidence])


@dataclass(frozen=True, init=False)
class ToolResultContractV1:
    version: str
    _schema_json: str | None

    def __init__(self, version, output_schema=None):
        object.__setattr__(self, 'version', reference(version))
        object.__setattr__(self, '_schema_json', schema_snapshot(output_schema))

    @property
    def output_schema(self):
        return None if self._schema_json is None else json.loads(self._schema_json)

    def to_dict(self):
        return dict(version=1, bindingVersion=self.version, outputSchema=self.output_schema)


@dataclass(frozen=True)
class ToolDisplayFieldV1:
    source: str
    pointer: str

    def __post_init__(self):
        if type(self.source) is not str or self.source not in {'arguments', 'result'}:
            raise ValueError('invalid tool display source')
        pointer(self.pointer)

    def to_dict(self):
        self.__post_init__()
        return asdict(self)


@dataclass(frozen=True, init=False)
class ToolEventPolicyV1:
    version: str
    fields: object

    def __init__(self, version, fields):
        reference(version)
        if not isinstance(fields, Mapping) or len(fields) > 8:
            raise ValueError('invalid tool display fields')
        snapshot = {}
        for name, field in fields.items():
            reference(name, 64)
            if type(field) is not ToolDisplayFieldV1:
                raise ValueError('invalid tool display declaration')
            snapshot[name] = ToolDisplayFieldV1(field.source, field.pointer)
        object.__setattr__(self, 'version', version)
        object.__setattr__(self, 'fields', MappingProxyType(snapshot))

    def to_dict(self):
        return dict(version=1, bindingVersion=self.version, fields={name: field.to_dict() for name, field in self.fields.items()})


@dataclass(frozen=True)
class EvidenceUseV1:
    scope: InvocationScope
    purpose: str
    source_key: str
    source_revision: str | None
    locator: str
    selector: str
    content_sha256: str
    call_id: str

    def __post_init__(self):
        if type(self.scope) is not InvocationScope:
            raise ValueError('invalid evidence scope')
        self.scope.__post_init__()
        if self.purpose not in {'modelUse', 'cite', 'retainedRead'}:
            raise ValueError('invalid evidence purpose')
        EvidenceDraftV1(self.source_key, self.source_revision, self.locator, self.selector, 'Evidence')
        reference(self.call_id)
        if type(self.content_sha256) is not str or re.fullmatch('[0-9a-f]{64}', self.content_sha256) is None:
            raise ValueError('invalid evidence hash')


@dataclass(frozen=True)
class ToolDisplayUseV1:
    scope: InvocationScope
    tool_name: str
    tool_version: str
    call_id: str
    phase: str
    fields: tuple
    purpose: str = 'toolProgress'

    def __post_init__(self):
        if type(self.scope) is not InvocationScope or self.purpose != 'toolProgress':
            raise ValueError('invalid display use')
        self.scope.__post_init__()
        for value in (self.tool_name, self.tool_version, self.call_id):
            reference(value)
        if self.phase not in {'started', 'completed', 'rejected', 'replayed', 'uncertain'}:
            raise ValueError('invalid display phase')
        if type(self.fields) is not tuple or len(self.fields) > 8:
            raise ValueError('invalid display fields')
        for name in self.fields:
            reference(name, 64)


@dataclass(frozen=True)
class EvidenceAuthorizerV1:
    identity: str
    version: str
    authorize: object

    def __post_init__(self):
        reference(self.identity); reference(self.version)
        if not callable(self.authorize):
            raise ValueError('invalid evidence authorizer')

    def to_dict(self):
        self.__post_init__()
        return dict(identity=self.identity, version=self.version)


@dataclass(frozen=True)
class ToolDisplayAuthorizerV1(EvidenceAuthorizerV1):
    """Distinct host grant: permission to execute does not imply display."""


@dataclass(frozen=True)
class CitationPolicyV1:
    mode: str

    def __post_init__(self):
        if type(self.mode) is not str or self.mode not in {'optional', 'required'}:
            raise ValueError('invalid citation policy')

    def to_dict(self):
        self.__post_init__()
        return dict(version=1, mode=self.mode)


@dataclass(frozen=True)
class ToolContractBindingsV1:
    identity: str
    version: str
    scope: InvocationScope
    evidence_authorizer: EvidenceAuthorizerV1 | None = None
    display_authorizer: ToolDisplayAuthorizerV1 | None = None
    citations: CitationPolicyV1 | None = None
    retained: tuple = ()
    limits: ToolContractLimitsV1 = ToolContractLimitsV1()

    def __post_init__(self):
        reference(self.identity); reference(self.version)
        if type(self.scope) is not InvocationScope or type(self.limits) is not ToolContractLimitsV1:
            raise ValueError('invalid tool contract binding')
        self.scope.__post_init__(); self.limits.__post_init__()
        if self.evidence_authorizer is not None and type(self.evidence_authorizer) is not EvidenceAuthorizerV1:
            raise ValueError('invalid evidence authorizer')
        if self.display_authorizer is not None and type(self.display_authorizer) is not ToolDisplayAuthorizerV1:
            raise ValueError('invalid display authorizer')
        if self.citations is not None:
            if type(self.citations) is not CitationPolicyV1 or self.evidence_authorizer is None:
                raise ValueError('citations require an evidence authorizer')
            self.citations.__post_init__()
        if type(self.retained) is not tuple or self.retained:
            raise ValueError('retained evidence requires verified imports')

    def to_dict(self):
        self.__post_init__()
        return dict(version=1, identity=self.identity, bindingVersion=self.version, scope=asdict(self.scope),
            evidenceAuthorizer=None if self.evidence_authorizer is None else self.evidence_authorizer.to_dict(),
            displayAuthorizer=None if self.display_authorizer is None else self.display_authorizer.to_dict(),
            citations=None if self.citations is None else self.citations.to_dict(), retained=[], limits=self.limits.to_dict())
