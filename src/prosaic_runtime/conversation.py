"""Immutable, bounded provider-neutral conversation records.

These records contain context, never executable authority. Factories validate
standard JSON before copying it; callers receive a fresh decoded copy.
"""
from dataclasses import dataclass, fields
import json
import math
import re
from .operation_context import bounded_reference

from .types import Result


FAILURE_CODES = frozenset({
    'providerFailure', 'invalidToolRequest', 'cancelled', 'timeout', 'contextLimit',
    'inputLimit', 'outputLimit', 'budgetExceeded', 'accountingFailure',
    'observationFailure', 'toolEffectUncertain', 'toolJournalFailure', 'toolFailure',
    'incompleteGeneration', 'bindingMismatch', 'interrupted',
})


class TurnAdmissionError(ValueError):
    """Closed safe admission failure; no original parser or prompt text."""
    def __init__(self, code, limit=None):
        if code not in {'invalidHistory', 'invalidArtifact', 'invalidPolicy',
                       'bindingMismatch', 'unsupportedCapability', 'inputLimit', 'contextLimit'}:
            raise ValueError('invalid admission code')
        self.code, self.limit = code, limit
        super().__init__(code)


@dataclass(frozen=True)
class TurnLimits:
    max_user_bytes: int = 16_384
    max_assistant_bytes: int = 65_536
    max_turn_bytes: int = 131_072
    max_input_bytes: int = 196_608
    max_history_bytes: int = 4_194_304
    max_json_depth: int = 64
    max_json_nodes: int = 65_536
    max_metadata_bytes: int = 65_536
    max_event_bytes: int = 65_536
    max_evidence_bytes: int = 4_194_304

    def __post_init__(self):
        if any(type(getattr(self, f.name)) is not int or getattr(self, f.name) <= 0 for f in fields(self)):
            raise ValueError('turn limits must be positive integers')
        # Python's encoder is recursive. A configurable traversal limit cannot
        # enlarge its safety ceiling.
        if self.max_json_depth > 64:
            raise ValueError('maximum JSON depth is 64')


def encode_owned(value, *, max_bytes=4_194_304, max_depth=64, max_nodes=65_536):
    """Bound traversed occurrences (including aliases) before serialization."""
    stack = [(value, 0, frozenset())]
    count, text_bytes = 0, 0
    while stack:
        child, level, ancestors = stack.pop()
        count += 1
        if count > max_nodes:
            raise ValueError('JSON node limit')
        kind = type(child)
        if kind in (dict, list):
            if level >= max_depth or id(child) in ancestors:
                raise ValueError('JSON nesting or cycle')
            if len(child) + count > max_nodes:
                raise ValueError('JSON node limit')
            next_ancestors = ancestors | {id(child)}
            if kind is dict:
                for key, item in child.items():
                    if type(key) is not str:
                        raise ValueError('JSON keys must be strings')
                    text_bytes += len(key.encode('utf-8', errors='strict'))
                    stack.append((item, level + 1, next_ancestors))
            else:
                stack.extend((item, level + 1, next_ancestors) for item in child)
        elif kind is str:
            text_bytes += len(child.encode('utf-8', errors='strict'))
        elif kind is float:
            if not math.isfinite(child):
                raise ValueError('nonfinite JSON')
        elif child is not None and kind not in (int, bool):
            raise ValueError('not standard JSON')
        if text_bytes > max_bytes:
            raise ValueError('JSON byte limit')
    try:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, UnicodeError, RecursionError, OverflowError, ValueError) as exc:
        raise ValueError('invalid bounded JSON') from exc
    if len(encoded) > max_bytes:
        raise ValueError('JSON byte limit')
    return encoded


def _closed(value, required, optional=()):
    if type(value) is not dict or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise ValueError('invalid record fields')


def _reference(value):
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', value):
        raise ValueError('invalid opaque reference')


def _message(value):
    _closed(value, {'role', 'content'}, {'turnStatus', 'failureCode'})
    role, content = value['role'], value['content']
    if type(role) is not str or role not in {'user', 'assistant', 'tool'} or type(content) is not list or not content:
        raise ValueError('invalid message')
    if role == 'user':
        if type(value.get('turnStatus')) is not str or value['turnStatus'] not in {'completed', 'failed'}:
            raise ValueError('invalid user status')
        if value['turnStatus'] == 'failed' and (type(value.get('failureCode')) is not str or value['failureCode'] not in FAILURE_CODES):
            raise ValueError('invalid failure code')
        if value['turnStatus'] == 'completed' and 'failureCode' in value:
            raise ValueError('completed message has failure')
    elif {'turnStatus', 'failureCode'} & value.keys():
        raise ValueError('status belongs to user message')
    for block in content:
        if type(block) is not dict:
            raise ValueError('invalid content block')
        kind = block.get('type')
        if kind == 'text' and role != 'tool':
            _closed(block, {'type', 'text'})
            if type(block['text']) is not str:
                raise ValueError('invalid text')
        elif kind == 'toolCall' and role == 'assistant':
            _closed(block, {'type', 'callId', 'name', 'arguments'})
            _reference(block['callId'])
            if type(block['name']) is not str or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', block['name']):
                raise ValueError('invalid tool name')
            if type(block['arguments']) is not dict:
                raise ValueError('invalid tool arguments')
        elif kind == 'toolOutcome' and role == 'tool':
            _closed(block, {'type', 'callId', 'outcome'})
            _reference(block['callId'])
            if (type(block['outcome']) is not dict or type(block['outcome'].get('status')) is not str or
                    block['outcome']['status'] not in {'ok', 'error'}):
                raise ValueError('invalid tool outcome')
        else:
            raise ValueError('unsupported content block')


@dataclass(frozen=True, init=False)
class _OwnedRecord:
    _encoded: bytes

    @classmethod
    def _create(cls, encoded):
        instance = object.__new__(cls)
        object.__setattr__(instance, '_encoded', encoded)
        return instance

    def to_dict(self):
        return json.loads(self._encoded)


class ConversationMessage(_OwnedRecord):
    @classmethod
    def from_dict(cls, document):
        encoded = encode_owned(document)
        _message(json.loads(encoded))
        return cls._create(encoded)

    @property
    def role(self):
        return self.to_dict()['role']


def _validate_groups(messages, *, single=False):
    active, final, pending = False, False, set()
    user_count = 0
    for message in messages:
        _message(message)
        role = message['role']
        if role == 'user':
            if pending or (active and not final):
                raise ValueError('incomplete historical turn')
            user_count += 1
            if message['turnStatus'] == 'failed':
                if single:
                    raise ValueError('completed turn cannot fail')
                active, final = False, True
            else:
                active, final = True, False
        elif not active or final:
            raise ValueError('orphan assistant or tool message')
        elif role == 'assistant':
            if pending:
                raise ValueError('missing tool outcomes')
            ids = [b['callId'] for b in message['content'] if b['type'] == 'toolCall']
            if len(set(ids)) != len(ids):
                raise ValueError('duplicate tool identity')
            pending = set(ids)
            final = not pending
        else:
            for block in message['content']:
                if block['callId'] not in pending:
                    raise ValueError('orphan tool outcome')
                pending.remove(block['callId'])
    if pending or (active and not final) or (single and user_count != 1):
        raise ValueError('incomplete historical turn')


class CompletedTurn(_OwnedRecord):
    @classmethod
    def from_dict(cls, document):
        encoded = encode_owned(document)
        value = json.loads(encoded)
        _closed(value, {'version', 'messages'})
        if type(value['version']) is not int or value['version'] != 1 or type(value['messages']) is not list:
            raise ValueError('invalid completed turn')
        _validate_groups(value['messages'], single=True)
        return cls._create(encoded)

    @property
    def messages(self):
        return tuple(ConversationMessage.from_dict(m) for m in self.to_dict()['messages'])


def validate_conversation_history(history, *, limits):
    if type(history) is not tuple or any(type(m) is not ConversationMessage for m in history):
        raise TurnAdmissionError('invalidHistory')
    try:
        # Records already own canonical bytes. Reject aggregate amplification
        # before decoding even a single caller-repeated record.
        size = 2
        if len(history) + 1 > limits.max_json_nodes:
            raise ValueError('history node limit')
        for index, message in enumerate(history):
            if type(message._encoded) is not bytes:
                raise ValueError('history encoding')
            size += len(message._encoded) + (1 if index else 0)
            if size > limits.max_history_bytes:
                raise ValueError('history byte limit')
        values, nodes = [], 1
        for message in history:
            value = message.to_dict()
            stack = [value]
            while stack:
                child = stack.pop()
                nodes += 1
                if nodes > limits.max_json_nodes:
                    raise ValueError('history node limit')
                if type(child) is dict:
                    stack.extend(child.values())
                elif type(child) is list:
                    stack.extend(child)
            values.append(value)
        encoded = encode_owned(values, max_bytes=limits.max_history_bytes,
                               max_depth=limits.max_json_depth, max_nodes=limits.max_json_nodes)
        owned = json.loads(encoded)
        _validate_groups(owned)
        return owned
    except (ValueError, TypeError, AttributeError, UnicodeError) as exc:
        raise TurnAdmissionError('invalidHistory') from exc


class TurnEvidence(_OwnedRecord):
    @classmethod
    def from_dict(cls, document):
        encoded = encode_owned(document)
        value = json.loads(encoded)
        _closed(value, {'version', 'providerAttempts', 'toolAttempts', 'knownUsage',
                        'usageComplete', 'toolOutcomes', 'failureCode', 'accounting'})
        if type(value['version']) is not int or value['version'] != 1:
            raise ValueError('invalid evidence version')
        for key in ('providerAttempts', 'toolAttempts'):
            if type(value[key]) is not int or value[key] < 0:
                raise ValueError('invalid attempt count')
        if (type(value['usageComplete']) is not bool or
                (value['failureCode'] is not None and (type(value['failureCode']) is not str or value['failureCode'] not in FAILURE_CODES))):
            raise ValueError('invalid evidence status')
        _closed(value['knownUsage'], (), {'inputTokens', 'outputTokens', 'totalTokens'})
        if any(type(n) is not int or n < 0 for n in value['knownUsage'].values()):
            raise ValueError('invalid known usage')
        if type(value['toolOutcomes']) is not list:
            raise ValueError('invalid effect evidence')
        for outcome in value['toolOutcomes']:
            _closed(outcome, {'callId', 'name', 'status'},
                    {'outcome', 'operationId', 'callIndex', 'signature', 'operationNamespace', 'operationKey'})
            _reference(outcome['callId'])
            _reference(outcome['name'])
            if type(outcome['status']) is not str or outcome['status'] not in {'completed', 'replayed', 'uncertain'}:
                raise ValueError('invalid effect status')
            if 'outcome' in outcome and type(outcome['outcome']) is not dict:
                raise ValueError('invalid effect outcome')
            if 'operationId' in outcome:
                _reference(outcome['operationId'])
            if 'callIndex' in outcome and (type(outcome['callIndex']) is not int or outcome['callIndex'] < 0):
                raise ValueError('invalid tool index')
            if 'signature' in outcome and (type(outcome['signature']) is not str or not re.fullmatch('[0-9a-f]{64}', outcome['signature'])):
                raise ValueError('invalid effect signature')
            if {'operationNamespace', 'operationKey'} & outcome.keys():
                if not bounded_reference(outcome.get('operationNamespace')) or not bounded_reference(outcome.get('operationKey'), 256):
                    raise ValueError('invalid logical effect address')
        if value['accounting'] is not None and type(value['accounting']) is not dict:
            raise ValueError('invalid accounting correlation')
        return cls._create(encoded)


@dataclass(frozen=True)
class TurnResult:
    result: Result
    completed_turn: CompletedTurn | None
    evidence: TurnEvidence
    context_report: object = None
    context_work: object = None


@dataclass(frozen=True, init=False)
class PreparedTurn:
    """Readmitted immutable prepared input; no public executable deserializer."""
    _encoded: bytes
    context_identity: str
    encoding_version: str
    input_bytes: int
    _context_work: object = None

    @classmethod
    def _create(cls, encoded, identity, input_bytes, *, encoding_version='conversation-turn-v1', context_work=None):
        instance = object.__new__(cls)
        object.__setattr__(instance, '_encoded', encoded)
        object.__setattr__(instance, 'context_identity', identity)
        object.__setattr__(instance, 'encoding_version', encoding_version)
        object.__setattr__(instance, 'input_bytes', input_bytes)
        object.__setattr__(instance, '_context_work', context_work)
        return instance

    @property
    def context_report(self):
        from .context_contracts import ContextAssemblyReport
        document = json.loads(self._encoded)
        return ContextAssemblyReport.from_dict(document['contextReport']) if 'contextReport' in document else None

    @property
    def context_selection(self):
        from .context_contracts import ContextSelection
        document = json.loads(self._encoded)
        return ContextSelection.from_dict(document['contextSelection']) if 'contextSelection' in document else None

    @property
    def context_work(self):
        from .context_contracts import ContextWorkState
        return ContextWorkState.from_dict(self._context_work.to_dict()) if self._context_work is not None else None
