"""Pure, bounded final-text provenance checks. No transport or source lookup."""
from dataclasses import dataclass
import hashlib
import json
import re

from .conversation import encode_owned
from .tool_contracts import CitationPolicyV1, ToolContractLimitsV1
from .tool_evidence import EligibleEvidenceV1

_HEX = re.compile(r'[0-9a-f]{64}\Z')
_MARKER = re.compile(rb'\[cite:([A-Za-z0-9_-]{1,128})\]\Z')
CITATION_INSTRUCTIONS = ('Evidence references identify captured source selections. '
    'Cite supplied references with the exact token [cite:<id>]. Use only supplied IDs. '
    'This syntax is reserved everywhere, including code and quoted text. '
    'References establish provenance; they do not guarantee that a claim follows from a source.')


class CitationViolation(RuntimeError):
    def __init__(self):
        super().__init__('citationViolation')


@dataclass(frozen=True, init=False)
class CitationsV1:
    _encoded: bytes

    def __init__(self, document):
        if (type(document) is not dict or set(document) != {'version', 'answerSha256', 'registrySha256', 'proof', 'markers'}
                or type(document['version']) is not int or document['version'] != 1
                or type(document['proof']) is not str or document['proof'] not in {'runtimeObserved', 'hostAttested'}
                or any(type(document[k]) is not str or not _HEX.fullmatch(document[k]) for k in ('answerSha256', 'registrySha256'))
                or type(document['markers']) is not list or len(document['markers']) > 128):
            raise ValueError('invalid citations sidecar')
        previous = 0
        for marker in document['markers']:
            if (type(marker) is not dict or set(marker) != {'start', 'end', 'evidenceId'} or
                    type(marker['start']) is not int or type(marker['end']) is not int or
                    not previous <= marker['start'] < marker['end'] or
                    type(marker['evidenceId']) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', marker['evidenceId'])):
                raise ValueError('invalid citation marker')
            previous = marker['end']
        object.__setattr__(self, '_encoded', encode_owned(document, max_bytes=32768))

    def to_dict(self):
        return json.loads(self._encoded)

    @classmethod
    def from_dict(cls, document):
        return cls(document)

    def verify_answer(self, text):
        if type(text) is not str:
            return False
        try:
            data = text.encode('utf-8')
        except UnicodeError:
            return False
        doc = self.to_dict()
        return hashlib.sha256(data).hexdigest() == doc['answerSha256'] and all(
            data[m['start']:m['end']] == f"[cite:{m['evidenceId']}]".encode() for m in doc['markers'])


def citation_markers(text, *, maximum=128):
    """The reserved prefix is recognized even in code/quoted text; offsets are bytes."""
    if type(text) is not str:
        raise CitationViolation()
    try:
        data = text.encode('utf-8')
    except UnicodeError:
        raise CitationViolation() from None
    markers, offset = [], 0
    while True:
        start = data.find(b'[cite', offset)
        if start < 0:
            break
        end = data.find(b']', start, start + 136)
        if end < 0:
            raise CitationViolation()
        end += 1
        token = _MARKER.fullmatch(data[start:end])
        if token is None or len(markers) >= maximum:
            raise CitationViolation()
        markers.append(dict(start=start, end=end, evidenceId=token.group(1).decode('ascii')))
        offset = end
    return markers


def validate_citations(text, registry, policy, *, limits=ToolContractLimitsV1()):
    if type(registry) is not EligibleEvidenceV1 or type(policy) is not CitationPolicyV1 or type(limits) is not ToolContractLimitsV1:
        raise ValueError('typed verified citation bindings required')
    policy.__post_init__(); limits.__post_init__()
    markers = citation_markers(text, maximum=limits.max_citations)
    eligible = frozenset(registry.ids)
    if any(marker['evidenceId'] not in eligible for marker in markers) or policy.mode == 'required' and (not text.strip() or not markers):
        raise CitationViolation()
    document = dict(version=1, answerSha256=hashlib.sha256(text.encode()).hexdigest(),
        registrySha256=registry.registry_digest, proof=registry.proof, markers=markers)
    try:
        encode_owned(document, max_bytes=limits.max_citation_bytes)
        return CitationsV1(document)
    except (ValueError, TypeError, UnicodeError):
        raise CitationViolation() from None
