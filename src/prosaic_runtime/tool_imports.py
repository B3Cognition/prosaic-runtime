"""Trusted-host captured imports. JSON deserialization alone supplies no authority."""
from dataclasses import asdict, dataclass
import hashlib
import json
import re

from .conversation import encode_owned
from .operation_context import InvocationScope
from .tool_contracts import EvidenceDraftV1, owned
from .tool_result_contract import select_pointer


def _hash(value):
    return hashlib.sha256(encode_owned(value)).hexdigest()


def validate_import_descriptor(value):
    keys = {'version', 'scope', 'source', 'evidenceId', 'originKind', 'originReceiptSha256', 'modelViewSha256',
        'contentSha256', 'contentBytes', 'contentKind', 'contentPointer', 'referencePointer', 'lineage'}
    if (type(value) is not dict or set(value) != keys or type(value['version']) is not int or value['version'] != 1 or
            type(value['evidenceId']) is not str or not re.fullmatch(r'(?:ctx|ev)_[0-9a-f]{64}', value['evidenceId']) or
            type(value['originKind']) is not str or value['originKind'] not in {'contextPack', 'retainedReceipt'} or
            type(value['contentKind']) is not str or value['contentKind'] not in {'text', 'json'} or
            type(value['contentBytes']) is not int or not 0 <= value['contentBytes'] <= 4_194_304):
        raise ValueError('invalid host import descriptor')
    InvocationScope(**value['scope'])
    EvidenceDraftV1.from_dict(value['source'])
    from .tool_contracts import pointer
    pointer(value['contentPointer']); pointer(value['referencePointer'])
    for key in ('originReceiptSha256', 'modelViewSha256', 'contentSha256'):
        if type(value[key]) is not str or not re.fullmatch(r'[0-9a-f]{64}', value[key]):
            raise ValueError('invalid host import digest')
    if value['lineage'] is not None and (type(value['lineage']) is not str or not re.fullmatch(r'(?:ctx|ev)_[0-9a-f]{64}', value['lineage'])):
        raise ValueError('invalid host import lineage')
    return owned(value, 16384)


@dataclass(frozen=True, init=False)
class HostEvidenceImportV1:
    _encoded: bytes
    _model_view: bytes

    def __init__(self, scope, source, *, evidence_id, origin_kind, origin_receipt_sha256,
                 model_view, content_pointer, reference_pointer, lineage=None):
        if type(scope) is not InvocationScope or type(source) is not EvidenceDraftV1 or type(model_view) is not dict:
            raise ValueError('typed captured host import required')
        view = owned(model_view)
        if select_pointer(view, reference_pointer) != evidence_id:
            raise ValueError('import reference mismatch')
        content = select_pointer(view, content_pointer)
        raw = content.encode('utf-8') if type(content) is str else encode_owned(content)
        descriptor = validate_import_descriptor(dict(version=1, scope=asdict(scope), source=source.to_dict(),
            evidenceId=evidence_id, originKind=origin_kind, originReceiptSha256=origin_receipt_sha256,
            modelViewSha256=_hash(view), contentSha256=hashlib.sha256(raw).hexdigest(), contentBytes=len(raw),
            contentKind='text' if type(content) is str else 'json', contentPointer=content_pointer,
            referencePointer=reference_pointer, lineage=lineage))
        object.__setattr__(self, '_encoded', encode_owned(descriptor))
        object.__setattr__(self, '_model_view', encode_owned(view))

    @property
    def evidence_id(self):
        return self.to_dict()['evidenceId']

    @property
    def scope(self):
        return InvocationScope(**self.to_dict()['scope'])

    @property
    def model_view(self):
        return json.loads(self._model_view)

    def to_dict(self):
        return json.loads(self._encoded)


def import_record(descriptor):
    descriptor = validate_import_descriptor(descriptor)
    return dict(version=1, originKind=descriptor['originKind'], scope=descriptor['scope'],
        id=descriptor['evidenceId'], **descriptor['source'], callId='import_' + _hash(descriptor),
        contentSha256=descriptor['contentSha256'], contentBytes=descriptor['contentBytes'],
        contentKind=descriptor['contentKind'], observation='retainedContext', importIdentity=_hash(descriptor),
        originReceiptSha256=descriptor['originReceiptSha256'], lineage=descriptor['lineage'])
