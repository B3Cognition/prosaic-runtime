from dataclasses import replace
import pytest
import prosaic_runtime as api
from prosaic_runtime.tool_evidence import EvidenceRegistryV1, EvidenceUseDenied
from test_tool_evidence import bindings, deliver


def imported(*, kind='contextPack', scope=None, value='exact selected bytes', origin='a' * 64):
    return api.HostEvidenceImportV1(scope or bindings().scope,
        api.EvidenceDraftV1('graph', 'r1', 'PRIVATE-LOCATOR', '', 'Selected context'),
        evidence_id='ctx_' + 'b' * 64, origin_kind=kind, origin_receipt_sha256=origin,
        model_view={'citationId': 'ctx_' + 'b' * 64, 'content': value},
        content_pointer='/content', reference_pointer='/citationId')


def test_exact_initial_pack_representation_needs_real_delivery():
    source = imported()
    registry = EvidenceRegistryV1(replace(bindings(), retained=(source,)))
    assert registry.eligible().ids == ()
    deliver(registry, {'message': source.model_view})
    assert registry.eligible().ids == (source.evidence_id,)
    record = registry.to_dict()['records'][0]
    assert record['contentKind'] == 'text' and record['originKind'] == 'contextPack'
    assert 'exact selected bytes' not in str(registry.to_dict())


def test_matching_id_without_exact_imported_representation_is_insufficient():
    source = imported()
    registry = EvidenceRegistryV1(replace(bindings(), retained=(source,)))
    view = source.model_view
    view['content'] = 'summary'
    deliver(registry, view)
    assert registry.eligible().ids == ()


def test_import_cannot_cross_scope_or_come_from_model_json():
    source = imported(scope=replace(bindings().scope, invocation_id='other'))
    with pytest.raises(ValueError): replace(bindings(), retained=(source,))
    with pytest.raises(ValueError): replace(bindings(), retained=(imported().to_dict(),))


def test_retained_import_uses_current_grant_and_new_submission():
    source = imported(kind='retainedReceipt')
    registry = EvidenceRegistryV1(replace(bindings(lambda use: False), retained=(source,)))
    with pytest.raises(EvidenceUseDenied): deliver(registry, source.model_view)
    assert registry.to_dict()['records'] == [] and registry.eligible().ids == ()


def test_import_descriptor_owns_bytes_and_identity():
    source = imported()
    view = source.model_view
    view['content'] = 'modified'
    assert source.model_view['content'] == 'exact selected bytes'
    assert source.to_dict() != imported(value='other').to_dict()
    assert source.to_dict() != imported(origin='c' * 64).to_dict()


def test_import_requires_explicit_reference_and_selection():
    with pytest.raises(ValueError): api.HostEvidenceImportV1(bindings().scope,
        api.EvidenceDraftV1('graph', None, '', '', 'Selected'), evidence_id='ctx_missing',
        origin_kind='contextPack', origin_receipt_sha256='a' * 64, model_view={'content': 'text'},
        content_pointer='/content', reference_pointer='/citationId')


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_imports_roundtrip_with_actual_initial_payload_and_citation(provider):
    from support.conversation_server import ConversationFixture
    source = imported()
    bound = replace(bindings(), retained=(source,), citations=api.CitationPolicyV1('required'))
    with ConversationFixture(provider) as fx:
        fx.queue_text(f'Checked [cite:{source.evidence_id}]')
        turn = fx.runtime.turn(fx.agent, message='Review', input_data={'selected': source.model_view},
            policy=fx.policy, tool_contract_bindings=bound)
        assert turn.result.exit_code == 0 and len(fx.requests) == 1
        assert 'PRIVATE-LOCATOR' not in fx.raw_requests[0].decode()
        assert api.TurnEvidence.from_dict(turn.evidence.to_dict()).to_dict() == turn.evidence.to_dict()
        assert turn.evidence.to_dict()['toolContractV1']['citations']['proof'] == 'runtimeObserved'
