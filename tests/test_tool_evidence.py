from dataclasses import replace
import pytest
import prosaic_runtime as api
from prosaic_runtime.conversation import encode_owned
from prosaic_runtime.tool_evidence import EvidenceRegistryV1, EvidenceUseDenied, digest

SCOPE = api.InvocationScope('invocation', run_id='run', operation_namespace='tenant')


def bindings(grant=lambda use: True, **kw):
    return api.ToolContractBindingsV1('host', '1', SCOPE,
        evidence_authorizer=api.EvidenceAuthorizerV1('sources', '1', grant), **kw)


def tool():
    return api.CustomTool('lookup', 'Lookup', {'type': 'object', 'additionalProperties': False}, lambda a: None, '1',
        result_contract=api.ToolResultContractV1('1', {'type': 'object'}))


def outcome(value=14, selector='/count', revision='r1'):
    return {'status': 'ok', 'result': api.ToolResultV1(data={'count': value, 'secret': 'PRIVATE'}, evidence=(
        api.EvidenceDraftV1('graph', revision, 'private:source', selector, 'Dependents'),)).to_dict()}


def stage(registry, raw=None, checked=None, call='call'):
    raw = outcome() if raw is None else raw
    return registry.stage(tool(), raw, checked or raw, call_id=call, call_index=0, arguments_sha256='a' * 64)


def deliver(registry, frame):
    payload = {'messages': [{'role': 'tool', 'content': __import__('json').dumps(frame)}]}
    ticket = registry.prepare_delivery(payload)
    registry.begin_delivery(ticket, 'request')
    registry.confirm_delivery(ticket, 'request')
    return ticket


def test_stage_is_not_delivery():
    registry = EvidenceRegistryV1(bindings())
    frame = stage(registry)
    assert registry.eligible().ids == ()
    ticket = registry.prepare_delivery({'messages': [frame]})
    assert registry.eligible().ids == ()
    registry.begin_delivery(ticket, 'request')
    assert registry.eligible().ids == ()
    registry.confirm_delivery(ticket, 'request')
    assert registry.eligible().ids == (frame['result']['evidence'][0]['id'],)


def test_ids_bind_scope_call_selector_and_value():
    def identity(binding, raw, call):
        return stage(EvidenceRegistryV1(binding), raw, call=call)['result']['evidence'][0]['id']
    base = identity(bindings(), outcome(), 'call')
    assert all(base != changed for changed in [identity(replace(bindings(), scope=replace(SCOPE, invocation_id='other')), outcome(), 'call'),
        identity(bindings(), outcome(), 'other'), identity(bindings(), outcome(15), 'call'),
        identity(bindings(), outcome(revision='r2'), 'call'), identity(bindings(), outcome(selector=''), 'call')])


def test_hash_is_computed_from_captured_selection():
    registry = EvidenceRegistryV1(bindings())
    frame = stage(registry)
    record = registry.to_dict()['records'][0]
    assert record['contentSha256'] == digest(14)
    assert record['contentBytes'] == len(encode_owned(14))
    assert 'private:source' not in str(frame) and 'PRIVATE' in str(frame)


def test_cross_scope_id_is_not_authority():
    first, second = EvidenceRegistryV1(bindings()), EvidenceRegistryV1(replace(bindings(), scope=replace(SCOPE, invocation_id='other')))
    frame = stage(first)
    deliver(second, frame)
    assert second.eligible().ids == ()


@pytest.mark.parametrize('grant', [lambda use: False, lambda use: 1, lambda use: (_ for _ in ()).throw(Exception('PRIVATE'))])
def test_authorizer_exception_denies(grant):
    registry = EvidenceRegistryV1(bindings(grant))
    with pytest.raises(EvidenceUseDenied, match='^evidenceUseDenied$'):
        stage(registry)
    assert registry.eligible().ids == ()


def test_all_three_permission_purposes_are_independent():
    purposes = []
    registry = EvidenceRegistryV1(bindings(lambda use: purposes.append(use.purpose) or use.purpose != 'retainedRead'))
    frame = stage(registry)
    deliver(registry, frame)
    evidence_id = frame['result']['evidence'][0]['id']
    registry.authorize(evidence_id, 'cite')
    with pytest.raises(EvidenceUseDenied): registry.authorize(evidence_id, 'retainedRead')
    assert purposes == ['modelUse', 'modelUse', 'modelUse', 'cite', 'retainedRead']


def test_unchanged_selection_retains_reference_after_guard_rewrite():
    raw = outcome()
    checked = outcome()
    checked['result']['data']['secret'] = 'changed'
    assert len(stage(EvidenceRegistryV1(bindings()), raw, checked)['result']['evidence']) == 1
    checked['result']['data']['count'] = 15
    assert stage(EvidenceRegistryV1(bindings()), raw, checked)['result']['evidence'] == []


def test_compacted_or_forged_projection_has_no_delivery():
    registry = EvidenceRegistryV1(bindings())
    frame = stage(registry)
    frame['result']['data']['count'] = 999
    deliver(registry, frame)
    assert registry.eligible().ids == ()


def test_failed_delivery_cannot_confirm_wrong_request():
    registry = EvidenceRegistryV1(bindings())
    frame = stage(registry)
    ticket = registry.prepare_delivery(frame)
    registry.begin_delivery(ticket, 'request')
    with pytest.raises(ValueError): registry.confirm_delivery(ticket, 'other')
    assert registry.eligible().ids == ()


def test_registry_and_sidecar_limits():
    registry = EvidenceRegistryV1(bindings(limits=api.ToolContractLimitsV1(max_registry_items=1)))
    stage(registry)
    with pytest.raises(ValueError): stage(registry, call='other')
    tiny = EvidenceRegistryV1(bindings(limits=api.ToolContractLimitsV1(max_evidence_bytes=100)))
    with pytest.raises(ValueError): stage(tiny)


def test_pointer_escapes_and_null_revision():
    registry = EvidenceRegistryV1(bindings())
    raw = {'status': 'ok', 'result': api.ToolResultV1(data={'a/b': {'~': 14}}, evidence=(
        api.EvidenceDraftV1('graph', None, '', '/a~1b/~0', 'Count'),)).to_dict()}
    frame = stage(registry, raw)
    deliver(registry, frame)
    assert registry.to_dict()['records'][0]['contentSha256'] == digest(14)
