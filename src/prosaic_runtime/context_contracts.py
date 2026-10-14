"""Optional owned context-admission contracts; no executable serialization."""
from dataclasses import dataclass, fields
import json
import re

from .conversation import TurnAdmissionError, encode_owned, _OwnedRecord
from .operation_context import bounded_reference


MAX_INTEGER = 2**63 - 1
FRAMING_REVISIONS = {'openai-compatible': 'openai-chat-v1',
                     'anthropic': 'anthropic-messages-v1'}


def _camel(name):
    head, *tail = name.split('_')
    return head + ''.join(piece.title() for piece in tail)


def _integer(value, minimum=0, maximum=MAX_INTEGER):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('integer bound')


def _digest(value):
    if type(value) is not str or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('digest')


def _reference(value):
    if not bounded_reference(value):
        raise ValueError('reference')


def _closed(document, keys):
    if type(document) is not dict or document.keys() != set(keys):
        raise ValueError('closed fields')


def _owned(document, *, maximum=16_384, nodes=512):
    return json.loads(encode_owned(document, max_bytes=maximum,
                                   max_depth=16, max_nodes=nodes))


def _range(value):
    if value is None:
        return 0
    _closed(value, ('startTurn', 'endTurnExclusive'))
    _integer(value['startTurn'], 1)
    _integer(value['endTurnExclusive'], value['startTurn'] + 1)
    return value['endTurnExclusive'] - value['startTurn']


class _DocumentMixin:
    def to_dict(self):
        try:
            document = {_camel(f.name): getattr(self, f.name) for f in fields(self)}
            return _owned(document)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise TurnAdmissionError('invalidPolicy') from None

    @classmethod
    def from_dict(cls, document):
        try:
            document = _owned(document)
            names = {_camel(f.name): f.name for f in fields(cls)}
            _closed(document, names)
            return cls(**{names[key]: value for key, value in document.items()})
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise TurnAdmissionError('invalidPolicy') from None


@dataclass(frozen=True)
class ContextPolicy(_DocumentMixin):
    mode: str
    context_window_tokens: int
    reserved_output_tokens: int
    safety_margin_tokens: int
    counter_identity: str
    max_candidate_checks: int = 128
    max_assembly_bytes: int = 33_554_432
    assembly_timeout_ms: int = 2_000

    def __post_init__(self):
        try:
            if type(self.mode) is not str or self.mode not in {'fullHistory', 'omitOldestTurns'}:
                raise ValueError('mode')
            _integer(self.context_window_tokens, 1)
            _integer(self.reserved_output_tokens, 1)
            _integer(self.safety_margin_tokens)
            _integer(self.max_candidate_checks, 2, 256)
            _integer(self.max_assembly_bytes, 1, 268_435_456)
            _integer(self.assembly_timeout_ms, 1, 30_000)
            _reference(self.counter_identity)
            if self.reserved_output_tokens + self.safety_margin_tokens >= self.context_window_tokens:
                raise ValueError('input capacity')
            self.to_dict()
        except (ValueError, TypeError, UnicodeError):
            raise TurnAdmissionError('invalidPolicy') from None

    @property
    def input_capacity_tokens(self):
        return self.context_window_tokens - self.reserved_output_tokens - self.safety_margin_tokens


@dataclass(frozen=True)
class TokenCounterDescriptor(_DocumentMixin):
    identity: str
    provider: str
    model: str
    framing_revision: str
    context_capacity_tokens: int
    implementation_version: str
    tokenizer_assets_digest: str
    template_assets_digest: str
    counting_kind: str
    guarantee: str

    def __post_init__(self):
        try:
            for name in ('identity', 'model', 'implementation_version'):
                _reference(getattr(self, name))
            if (type(self.provider) is not str or self.provider not in FRAMING_REVISIONS
                    or self.framing_revision != FRAMING_REVISIONS[self.provider]):
                raise ValueError('unsupported framing')
            _integer(self.context_capacity_tokens, 1)
            _digest(self.tokenizer_assets_digest)
            _digest(self.template_assets_digest)
            if self.counting_kind not in {'exact', 'conservativeUpperBound'}:
                raise ValueError('counting guarantee')
            if (type(self.guarantee) is not str or not self.guarantee.strip()
                    or len(self.guarantee.encode('utf-8')) > 8192):
                raise ValueError('guarantee bound')
            self.to_dict()
        except (ValueError, TypeError, UnicodeError):
            raise TurnAdmissionError('invalidPolicy') from None


class _ContextRecord(_OwnedRecord):
    maximum = 16_384
    nodes = 512
    keys = frozenset()

    def __setattr__(self, name, value):
        raise AttributeError('immutable context record')

    def __delattr__(self, name):
        raise AttributeError('immutable context record')

    @classmethod
    def from_dict(cls, document):
        try:
            document = _owned(document, maximum=cls.maximum, nodes=cls.nodes)
            _closed(document, cls.keys)
            cls.validate(document)
            return cls._create(encode_owned(document, max_bytes=cls.maximum,
                max_depth=16, max_nodes=cls.nodes))
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise TurnAdmissionError('invalidPolicy') from None

    def __getattr__(self, name):
        key = _camel(name)
        if key not in self.keys:
            raise AttributeError(name)
        return self.to_dict()[key]


class ContextAssemblyReport(_ContextRecord):
    keys = frozenset({'version', 'mode', 'counterIdentity', 'sourceHistoryDigest',
        'selectedHistoryDigest', 'inputTokens', 'countingKind', 'reservedOutputTokens',
        'safetyMarginTokens', 'inputCapacityTokens', 'encodedInputBytes',
        'retainedTurnCount', 'omittedTurnCount', 'retainedRange', 'omittedRange',
        'candidateChecks', 'candidateBytes'})

    @staticmethod
    def validate(d):
        _integer(d['version'], 1, 1)
        if d['mode'] not in {'fullHistory', 'omitOldestTurns'} or d['countingKind'] not in {'exact', 'conservativeUpperBound'}:
            raise ValueError('report enum')
        _reference(d['counterIdentity'])
        _digest(d['sourceHistoryDigest'])
        _digest(d['selectedHistoryDigest'])
        for name in ('inputTokens', 'safetyMarginTokens', 'retainedTurnCount',
                     'omittedTurnCount', 'candidateBytes'):
            _integer(d[name])
        for name in ('reservedOutputTokens', 'inputCapacityTokens', 'encodedInputBytes', 'candidateChecks'):
            _integer(d[name], 1)
        if (d['inputTokens'] > d['inputCapacityTokens'] or
                _range(d['retainedRange']) != d['retainedTurnCount'] or
                _range(d['omittedRange']) != d['omittedTurnCount']):
            raise ValueError('report capacity/range')
        omitted = d['omittedTurnCount']
        if ((d['omittedRange'] is not None and d['omittedRange']['startTurn'] != 1) or
                (d['retainedRange'] is not None and d['retainedRange']['startTurn'] != omitted + 1) or
                (d['mode'] == 'fullHistory' and omitted)):
            raise ValueError('report partition')


class ContextSelection(_ContextRecord):
    maximum, nodes = 8192, 256
    keys = frozenset({'version', 'contextPolicyIdentity', 'counterDescriptorIdentity',
        'sourceHistoryDigest', 'selectedHistoryDigest', 'retainedRange',
        'omittedRange', 'initialReportIdentity'})

    @staticmethod
    def validate(d):
        _integer(d['version'], 1, 1)
        for name in ('contextPolicyIdentity', 'counterDescriptorIdentity',
                     'sourceHistoryDigest', 'selectedHistoryDigest', 'initialReportIdentity'):
            _digest(d[name])
        omitted = _range(d['omittedRange'])
        _range(d['retainedRange'])
        if ((d['omittedRange'] is not None and d['omittedRange']['startTurn'] != 1) or
                (d['retainedRange'] is not None and d['retainedRange']['startTurn'] != omitted + 1)):
            raise ValueError('selection partition')


class ContextWorkState(_ContextRecord):
    maximum, nodes = 4096, 128
    keys = frozenset({'version', 'candidateChecks', 'candidateBytes',
        'activeAssemblyMicros', 'reservedActiveMicros', 'readmissionCandidateBytes',
        'readmissionPending', 'checkpointSequence'})

    @staticmethod
    def validate(d):
        _integer(d['version'], 1, 1)
        for name in ContextWorkState.keys - {'version', 'readmissionPending'}:
            _integer(d[name])
        if type(d['readmissionPending']) is not bool:
            raise ValueError('readmission flag')
