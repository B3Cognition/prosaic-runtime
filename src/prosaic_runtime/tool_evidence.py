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


@dataclass(frozen=True, init=False)
class HostDeliveryAttestationV1:
    scope: object
    request_id: str
    _encoded: bytes

    def __init__(self, scope, request_id, native_payload):
        from .operation_context import InvocationScope
        if type(scope) is not InvocationScope:
            raise ValueError('invalid host attestation scope')
        scope.__post_init__(); reference(request_id)
        object.__setattr__(self, 'scope', scope)
        object.__setattr__(self, 'request_id', request_id)
        object.__setattr__(self, '_encoded', encode_owned(native_payload))


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
        self._calls = {}
        self._pending_events = {}
        self.citations = None
        self._imports_ready = False
        self._snapshot()  # Refuse an unrepresentable host budget before execution.

    def _snapshot(self, *, records=None, calls=None, deliveries=None):
        # All retained components share one budget, including JSON framing.
        document=dict(version=1,records=list((self._records if records is None else records).values()),
            calls=list((self._calls if calls is None else calls).values()),
            deliveries=self._delivered if deliveries is None else deliveries)
        try:
            return owned(document,self.bindings.limits.max_evidence_bytes)
        except ValueError:
            raise ToolResultValidationError('result_limit') from None

    def _register_imports(self):
        if self._imports_ready:
            return
        from .tool_imports import import_record
        records, projections = {}, {}
        for imported in self.bindings.retained:
            descriptor = imported.to_dict()
            record = import_record(descriptor)
            if record['id'] in records or record['id'] in self._records:
                raise ValueError('duplicate imported evidence')
            self._authorize(record, 'modelUse')
            records[record['id']] = record
            projections[descriptor['modelViewSha256']] = (record['id'],)
        candidate = {**self._records, **records}
        if len(candidate) > self.bindings.limits.max_registry_items:
            raise ValueError('evidence registry limit')
        self._snapshot(records=candidate)
        self._records = candidate
        self._projections.update(projections)
        self._imports_ready = True

    def capture_completion(self, tool, outcome, *, call_id, call_index, arguments_sha256, status):
        record = dict(callId=call_id, callIndex=call_index, toolName=tool.name, toolVersion=tool.version,
            argumentsSha256=arguments_sha256, status=status, outcome=outcome)
        candidate = {**self._calls, call_id: record}
        try:
            self._snapshot(calls=candidate)
        except ToolResultValidationError:
            # Completion is already known (and journaled where applicable).
            # The full private outcome remains in TurnCapture/the effect journal;
            # retain a bounded completion marker without creating eligible data.
            marker={**record,'outcome':None}
            try:
                self._snapshot(calls={**self._calls,call_id:marker})
            except ToolResultValidationError:
                pass
            else:
                self._calls[call_id]=marker
            raise
        self._calls = owned(candidate)

    def finish_event(self, call_id, checked=None):
        event = self._pending_events.pop(call_id, None)
        if event is not None:
            event.finish(checked)

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
        self._snapshot(records=all_records)
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
        self._register_imports()
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
        prospective=owned(self._delivered)
        for evidence_id in ticket.evidence_ids:
            prospective.setdefault(evidence_id,[]).append(dict(requestId=request_id,inputSha256=ticket.input_sha256))
        self._snapshot(deliveries=prospective)
        state[1] = request_id

    def confirm_delivery(self, ticket, request_id):
        state = self._tickets.get(getattr(ticket, 'identity', None))
        if state is None or state[0] is not ticket or state[1] != request_id or state[2]:
            raise ValueError('invalid delivery acknowledgment')
        candidate = owned(self._delivered)
        for evidence_id in ticket.evidence_ids:
            candidate.setdefault(evidence_id, []).append(dict(requestId=request_id, inputSha256=ticket.input_sha256))
        self._snapshot(deliveries=candidate)
        self._delivered = candidate
        state[2] = True

    def eligible(self):
        document = dict(version=1, scope=asdict(self.bindings.scope), records={
            evidence_id: dict(**self._records[evidence_id], deliveries=delivery)
            for evidence_id, delivery in self._delivered.items()})
        return EligibleEvidenceV1(document, 'runtimeObserved', _seal=_SEAL)

    def host_attested(self, attestation):
        if type(attestation) is not HostDeliveryAttestationV1 or attestation.scope != self.bindings.scope:
            raise ValueError('scoped host delivery attestation required')
        ticket = self.prepare_delivery(None, encoded_bytes=attestation._encoded)
        for evidence_id in ticket.evidence_ids:
            self.authorize(evidence_id, 'cite')
        document = dict(version=1, scope=asdict(self.bindings.scope), records={evidence_id:
            dict(**self._records[evidence_id], deliveries=[dict(requestId=attestation.request_id, inputSha256=ticket.input_sha256)])
            for evidence_id in ticket.evidence_ids})
        return EligibleEvidenceV1(document, 'hostAttested', _seal=_SEAL)

    def to_dict(self):
        return self._snapshot()


@dataclass(frozen=True, init=False)
class ToolInvocationEvidenceV1:
    _encoded: bytes

    def __init__(self, registry):
        if type(registry) is not EvidenceRegistryV1:
            raise ValueError('owned registry required')
        object.__setattr__(self, '_encoded', encode_owned(registry.to_dict(), max_bytes=registry.bindings.limits.max_evidence_bytes))

    def to_dict(self):
        return json.loads(self._encoded)


def validate_invocation_evidence(document, *, imports=()):
    """Data codec only. Reading a receipt never confers delivery authority."""
    document = owned(document, 65536)
    if type(document) is not dict or set(document) != {'version', 'records', 'deliveries', 'calls'} or type(document['version']) is not int or document['version'] != 1:
        raise ValueError('invalid invocation evidence')
    if type(document['records']) is not list or len(document['records']) > 256 or type(document['calls']) is not list or type(document['deliveries']) is not dict:
        raise ValueError('invalid evidence records')
    calls = {}
    for call in document['calls']:
        if type(call) is not dict or set(call) != {'callId', 'callIndex', 'toolName', 'toolVersion', 'argumentsSha256', 'status', 'outcome'}:
            raise ValueError('invalid captured call')
        for key in ('callId', 'toolName', 'toolVersion'):
            reference(call[key])
        if call['callId'] in calls or type(call['callIndex']) is not int or call['callIndex'] < 0 or call['status'] not in {'completed', 'replayed', 'uncertain'}:
            raise ValueError('invalid captured call origin')
        if call['outcome'] is not None and type(call['outcome']) is not dict:
            raise ValueError('invalid captured completion')
        calls[call['callId']] = call
    records = {}
    from .tool_imports import import_record
    imported_records = {item['evidenceId']: import_record(item) for item in imports}
    for record in document['records']:
        if type(record) is dict and record.get('originKind') in {'contextPack', 'retainedReceipt'}:
            if imported_records.get(record.get('id')) != record or record['id'] in records:
                raise ValueError('unbound imported source')
            records[record['id']] = record
            continue
        required = {'version', 'scope', 'callId', 'callIndex', 'toolName', 'toolVersion', 'argumentsSha256',
            'sourceKey', 'sourceRevision', 'locator', 'selector', 'label', 'bodySha256', 'contentSha256',
            'contentBytes', 'contentKind', 'observation', 'id'}
        if type(record) is not dict or set(record) != required:
            raise ValueError('invalid captured source')
        from .operation_context import InvocationScope
        InvocationScope(**record['scope'])
        draft = EvidenceDraftV1.from_dict({key: record[key] for key in ('sourceKey', 'sourceRevision', 'locator', 'selector', 'label')})
        if record['id'] != 'ev_' + digest({k: v for k, v in record.items() if k != 'id'}) or record['id'] in records:
            raise ValueError('invalid captured source identity')
        call = calls.get(record['callId'])
        if call is None or any(call[key] != record[key] for key in ('callIndex', 'toolName', 'toolVersion', 'argumentsSha256')):
            raise ValueError('missing captured source origin')
        outcome = call['outcome']
        if type(outcome) is not dict or outcome.get('status') != 'ok':
            raise ValueError('missing captured source body')
        value = outcome.get('result')
        if type(value) is not dict or value.get('version') != 1 or draft.to_dict() not in value.get('sources', []):
            raise ValueError('missing captured source declaration')
        selected = select_pointer(value['data'], record['selector'])
        if (record['contentKind'] != 'json' or record['contentSha256'] != digest(selected) or
                record['contentBytes'] != len(encode_owned(selected)) or record['bodySha256'] != digest(value['data']) or
                record['observation'] != ('journalReplay' if call['status'] == 'replayed' else 'callResult')):
            raise ValueError('captured source mismatch')
        records[record['id']] = record
    for evidence_id, deliveries in document['deliveries'].items():
        if evidence_id not in records or type(deliveries) is not list or not deliveries:
            raise ValueError('invalid source delivery')
        for delivery in deliveries:
            if type(delivery) is not dict or set(delivery) != {'requestId', 'inputSha256'}:
                raise ValueError('invalid delivery record')
            reference(delivery['requestId'])
            if type(delivery['inputSha256']) is not str or len(delivery['inputSha256']) != 64 or any(c not in '0123456789abcdef' for c in delivery['inputSha256']):
                raise ValueError('invalid delivery digest')
    return document
