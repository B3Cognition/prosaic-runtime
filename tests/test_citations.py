import hashlib
import pytest
import prosaic_runtime as api
from prosaic_runtime.citations import validate_citations, CitationViolation, CitationsV1
from prosaic_runtime.tool_evidence import EvidenceRegistryV1
from test_tool_evidence import bindings, stage, deliver


def eligible():
    registry = EvidenceRegistryV1(bindings())
    frame = stage(registry)
    deliver(registry, frame)
    return registry.eligible(), frame['result']['evidence'][0]['id']


def test_optional_validates_every_present_marker():
    registry, evidence_id = eligible()
    assert validate_citations('No citation', registry, api.CitationPolicyV1('optional')).to_dict()['markers'] == []
    with pytest.raises(CitationViolation):
        validate_citations(f'[cite:{evidence_id}] [cite:unknown]', registry, api.CitationPolicyV1('optional'))


@pytest.mark.parametrize('text', ['', 'No source', '[cite:unknown]'])
def test_required_needs_a_valid_marker(text):
    with pytest.raises(CitationViolation): validate_citations(text, eligible()[0], api.CitationPolicyV1('required'))


@pytest.mark.parametrize('text', ['[cite:', '[cite]', '[cite:]', '[cite:x', '[cite:x y]', '[cite:\u202efoo]'])
def test_malformed_and_truncated_tokens_fail_in_both_modes(text):
    for mode in ('optional', 'required'):
        with pytest.raises(CitationViolation): validate_citations(text, eligible()[0], api.CitationPolicyV1(mode))


def test_utf8_spans_select_exact_marker_bytes():
    registry, evidence_id = eligible()
    marker = f'[cite:{evidence_id}]'
    text = f'Žluťoučký 🦊 {marker} and {marker}'
    sidecar = validate_citations(text, registry, api.CitationPolicyV1('required'))
    document = sidecar.to_dict()
    assert document['answerSha256'] == hashlib.sha256(text.encode()).hexdigest()
    assert document['registrySha256'] == registry.registry_digest
    assert document['proof'] == 'runtimeObserved'
    assert len(document['markers']) == 2
    for span in document['markers']:
        assert text.encode()[span['start']:span['end']] == marker.encode()
    assert CitationsV1.from_dict(document).to_dict() == document
    assert sidecar.verify_answer(text) is True and sidecar.verify_answer(text + '!') is False


def test_tokens_in_code_blocks_follow_reserved_syntax():
    registry, evidence_id = eligible()
    assert validate_citations(f'```\n[cite:{evidence_id}]\n```', registry, api.CitationPolicyV1('required'))
    with pytest.raises(CitationViolation): validate_citations('`[cite:invented]`', registry, api.CitationPolicyV1('optional'))


def test_unknown_private_and_foreign_ids_fail():
    registry, evidence_id = eligible()
    empty = EvidenceRegistryV1(bindings()).eligible()
    with pytest.raises(CitationViolation): validate_citations(f'[cite:{evidence_id}]', empty, api.CitationPolicyV1('required'))
    with pytest.raises(ValueError): validate_citations('answer', registry.to_dict(), api.CitationPolicyV1('optional'))


def test_marker_and_sidecar_limits():
    registry, evidence_id = eligible()
    text = f'[cite:{evidence_id}]' * 128
    assert len(validate_citations(text, registry, api.CitationPolicyV1('required')).to_dict()['markers']) == 128
    with pytest.raises(CitationViolation): validate_citations(text + f'[cite:{evidence_id}]', registry, api.CitationPolicyV1('required'))
    with pytest.raises(CitationViolation): validate_citations(text, registry, api.CitationPolicyV1('required'),
        limits=api.ToolContractLimitsV1(max_citation_bytes=100))


def test_optional_empty_answer_and_owned_sidecar():
    registry, _ = eligible()
    sidecar = validate_citations('', registry, api.CitationPolicyV1('optional'))
    doc = sidecar.to_dict()
    doc['markers'].append({})
    assert sidecar.to_dict()['markers'] == []
    with pytest.raises(ValueError): CitationsV1.from_dict({**sidecar.to_dict(), 'unknown': True})
