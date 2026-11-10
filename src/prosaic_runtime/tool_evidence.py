"""Captured origin is independent from confirmed submission and current permission."""
from dataclasses import asdict, dataclass
import hashlib
import json
from uuid import uuid4

from jsonschema import Draft202012Validator
from .conversation import encode_owned
from .tool_contracts import EvidenceDraftV1, EvidenceUseV1, ToolContractBindingsV1, owned, reference
from .tool_result_contract import select_pointer, validate_tool_result_outcome, ToolResultValidationError


def digest(value):
    return hashlib.sha256(encode_owned(value)).hexdigest()


class EvidenceUseDenied(RuntimeError):
    def __init__(self):
        super().__init__('evidenceUseDenied')


_SEAL = object()


@dataclass(frozen=True, init=False)
class EligibleEvidenceV1:
    _encoded: bytes
    proof: str

    def __init__(self, document, proof, *, _seal=None):
        if _seal is not _SEAL or proof not in {'runtimeObserved', 'hostAttested'}:
            raise ValueError('verified evidence registry required')
        object.__setattr__(self, '_encoded', encode_owned(document, max_bytes=65536))
        object.__setattr__(self, 'proof', proof)

    @property
    def ids(self):
        return tuple(self.to_dict()['records'])

    def to_dict(self):
        return json.loads(self._encoded)

    @property
    def registry_digest(self):
        return hashlib.sha256(self._encoded).hexdigest()


@dataclass(frozen=True)
class _DeliveryTicket:
    identity: str
    input_sha256: str
    evidence_ids: tuple


class EvidenceRegistryV1:
    """No ambient registry or ID lookup: ownership stays with this invocation."""
    def __init__(self, bindings):
        if type(bindings) is not ToolContractBindingsV1:
            raise ValueError('invalid tool contract bindings')
        bindings.__post_init__()
        self.bindings = bindings
        self._records = {}
        self._projections = {}
        self._tickets = {}
        self._delivered = {}

    def _authorize(self, record, purpose):
        grant = self.bindings.evidence_authorizer
        if grant is None:
            raise EvidenceUseDenied()
        use = EvidenceUseV1(self.bindings.scope, purpose, record['sourceKey'], record['sourceRevision'],
            record['locator'], record['selector'], record['contentSha256'], record['callId'])
        try:
            allowed = grant.authorize(use) is True
        except Exception:
            allowed = False
        if not allowed:
            raise EvidenceUseDenied()

    def authorize(self, evidence_id, purpose):
        if purpose not in {'modelUse', 'cite', 'retainedRead'} or evidence_id not in self._records:
            raise EvidenceUseDenied()
        self._authorize(self._records[evidence_id], purpose)

    def stage(self, tool, raw_outcome, checked_outcome, *, call_id, call_index, arguments_sha256, replayed=False):
        """Use producer origins only; guards may edit data but cannot mint a source."""
        reference(call_id)
        if type(call_index) is not int or call_index < 0 or type(replayed) is not bool:
            raise ValueError('invalid call origin')
        if type(arguments_sha256) is not str or len(arguments_sha256) != 64 or any(c not in '0123456789abcdef' for c in arguments_sha256):
            raise ValueError('invalid argument digest')
        raw = validate_tool_result_outcome(tool, raw_outcome)
        checked = owned(checked_outcome, tool.max_result_bytes)
        if raw['status'] != 'ok' or checked.get('status') != 'ok':
            return checked
        value = checked.get('result')
        if type(value) is not dict or type(value.get('version')) is not int or value['version'] != 1 or 'data' not in value:
            raise ToolResultValidationError()
        schema = tool.result_contract.output_schema
        try:
            if schema is not None:
                Draft202012Validator(schema).validate(value['data'])
        except Exception:
            raise ToolResultValidationError() from None
        data = value['data']
        records = {}
        references = []
        sources = raw['result']['sources']
        if len(sources) > self.bindings.limits.max_evidence_items:
            raise ValueError('evidence declaration limit')
        for source in sources:
            draft = EvidenceDraftV1.from_dict(source)
            original = select_pointer(raw['result']['data'], draft.selector)
            try:
                selected = select_pointer(data, draft.selector)
            except ValueError:
                continue
            if encode_owned(original) != encode_owned(selected):
                continue
            record = dict(version=1, scope=asdict(self.bindings.scope), callId=call_id, callIndex=call_index,
                toolName=tool.name, toolVersion=tool.version, argumentsSha256=arguments_sha256,
                **draft.to_dict(), bodySha256=digest(raw['result']['data']),
                contentSha256=digest(original), contentBytes=len(encode_owned(original)), contentKind='json',
                observation='journalReplay' if replayed else 'callResult')
            evidence_id = 'ev_' + digest(record)
            record['id'] = evidence_id
            self._authorize(record, 'modelUse')
            records[evidence_id] = record
            references.append(dict(id=evidence_id, label=draft.label, observation=record['observation']))
        all_records = {**self._records, **records}
        if len(all_records) > self.bindings.limits.max_registry_items:
            raise ValueError('evidence registry limit')
        encode_owned(dict(version=1, records=list(all_records.values())), max_bytes=self.bindings.limits.max_evidence_bytes)
        projected = dict(status='ok', result=dict(version=1, data=data, evidence=references))
        # Preserve both historical JSON framing bounds.
        encode_owned(projected, max_bytes=tool.max_result_bytes)
        if len(json.dumps(projected, allow_nan=False).encode()) > tool.max_result_bytes:
            raise ToolResultValidationError('result_limit')
        self._records = all_records
        self._projections[digest(projected)] = tuple(records)
        return owned(projected)

    def prepare_delivery(self, native_payload, *, encoded_bytes=None):
        """Inspect the final wire body, after compaction/encoding, without trusting events."""
        if encoded_bytes is None:
            encoded_bytes = encode_owned(native_payload)
        if type(encoded_bytes) is not bytes:
            raise ValueError('invalid provider bytes')
        # The encoded document is authoritative even when the caller still has a
        # mutable pre-encoding payload. No independent prompt construction counts.
        native_payload = json.loads(encoded_bytes)
        candidates, stack = set(), [native_payload]
        while stack:
            node = stack.pop()
            if type(node) is dict:
                if node.get('status') == 'ok' and 'result' in node:
                    candidates.update(self._projections.get(digest(node), ()))
                stack.extend(node.values())
            elif type(node) is list:
                stack.extend(node)
            elif type(node) is str and node.lstrip().startswith(('{', '[')):
                try:
                    child = json.loads(node)
                except (ValueError, RecursionError):
                    continue
                stack.append(child)
        for evidence_id in sorted(candidates):
            self.authorize(evidence_id, 'modelUse')
        ticket = _DeliveryTicket(uuid4().hex, hashlib.sha256(encoded_bytes).hexdigest(), tuple(sorted(candidates)))
        self._tickets[ticket.identity] = [ticket, None, False]
        return ticket

    def begin_delivery(self, ticket, request_id):
        reference(request_id)
        state = self._tickets.get(getattr(ticket, 'identity', None))
        if state is None or state[0] is not ticket or state[1] is not None:
            raise ValueError('invalid delivery ticket')
        for evidence_id in ticket.evidence_ids:
            self.authorize(evidence_id, 'modelUse')
        state[1] = request_id

    def confirm_delivery(self, ticket, request_id):
        state = self._tickets.get(getattr(ticket, 'identity', None))
        if state is None or state[0] is not ticket or state[1] != request_id or state[2]:
            raise ValueError('invalid delivery acknowledgment')
        candidate = owned(self._delivered)
        for evidence_id in ticket.evidence_ids:
            candidate.setdefault(evidence_id, []).append(dict(requestId=request_id, inputSha256=ticket.input_sha256))
        encode_owned(dict(version=1, records=list(self._records.values()), deliveries=candidate),
            max_bytes=self.bindings.limits.max_evidence_bytes)
        self._delivered = candidate
        state[2] = True

    def eligible(self):
        document = dict(version=1, scope=asdict(self.bindings.scope), records={
            evidence_id: dict(**self._records[evidence_id], deliveries=delivery)
            for evidence_id, delivery in self._delivered.items()})
        return EligibleEvidenceV1(document, 'runtimeObserved', _seal=_SEAL)

    def to_dict(self):
        return owned(dict(version=1, records=list(self._records.values()), deliveries=self._delivered),
            self.bindings.limits.max_evidence_bytes)


@dataclass(frozen=True, init=False)
class ToolInvocationEvidenceV1:
    _encoded: bytes

    def __init__(self, registry):
        if type(registry) is not EvidenceRegistryV1:
            raise ValueError('owned registry required')
        object.__setattr__(self, '_encoded', encode_owned(registry.to_dict(), max_bytes=registry.bindings.limits.max_evidence_bytes))

    def to_dict(self):
        return json.loads(self._encoded)
