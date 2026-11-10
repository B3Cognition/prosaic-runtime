import pytest
import prosaic_runtime as api

SCHEMA = {'type': 'object', 'properties': {}, 'additionalProperties': False}


def test_event_policy_does_not_require_result_contract():
    policy = api.ToolEventPolicyV1('1', {'component': api.ToolDisplayFieldV1('arguments', '/component')})
    tool = api.CustomTool('lookup', 'Lookup', SCHEMA, lambda a: {'count': 14}, '1', event_policy=policy)
    assert tool.result_contract is None
    assert tool.descriptor['event_policy'] == policy.to_dict()
    assert 'result_contract' not in tool.descriptor


def test_owned_result_and_schema_cannot_be_mutated_through_inputs_or_views():
    schema = {'type': 'object', 'properties': {'count': {'type': 'integer'}}, 'additionalProperties': False}
    contract = api.ToolResultContractV1('1', schema)
    schema['properties'].clear()
    contract.output_schema['properties'].clear()
    assert 'count' in contract.output_schema['properties']
    data = {'count': 14}
    result = api.ToolResultV1(data=data, evidence=(api.EvidenceDraftV1('graph', None, 'component:x', '/count', 'Count'),))
    data['count'] = 99
    result.data['count'] = 8
    assert result.data == {'count': 14}
    assert result.to_dict()['sources'][0]['sourceRevision'] is None


def test_dictionary_is_never_a_typed_result():
    from prosaic_runtime.tools import bounded_result
    dictionary = {'version': 1, 'data': {'count': 14}, 'sources': []}
    assert bounded_result(dictionary, 65536) == {'status': 'ok', 'result': dictionary}
    assert type(api.ToolResultV1(data=dictionary)) is api.ToolResultV1


@pytest.mark.parametrize('source,pointer', [('private', '/x'), ('arguments', 'x'), ('result', '/bad~2escape')])
def test_invalid_display_selector_is_rejected(source, pointer):
    with pytest.raises(ValueError): api.ToolDisplayFieldV1(source, pointer)


@pytest.mark.parametrize('change', [{'max_event_bytes': 4097}, {'max_display_fields': 9},
    {'max_evidence_items': 65}, {'max_registry_items': 257}, {'max_citations': 129},
    {'max_event_bytes': True}, {'max_citation_bytes': 0}])
def test_contract_limits_only_narrow(change):
    with pytest.raises(ValueError): api.ToolContractLimitsV1(**change)


@pytest.mark.parametrize('bad', [float('nan'), {1: 'value'}, object()])
def test_non_json_result_is_rejected(bad):
    with pytest.raises(ValueError): api.ToolResultV1(data=bad)


def test_authorizers_are_versioned_and_not_serialized():
    auth = api.EvidenceAuthorizerV1('scope', '1', lambda use: True)
    binding = api.ToolContractBindingsV1('tools', '1', api.InvocationScope('invocation'),
        evidence_authorizer=auth, citations=api.CitationPolicyV1('required'))
    assert binding.to_dict()['evidenceAuthorizer'] == {'identity': 'scope', 'version': '1'}
    assert 'authorize' not in str(binding.to_dict())
    with pytest.raises(ValueError): api.CitationPolicyV1('always')
    with pytest.raises(ValueError): api.ToolContractBindingsV1('tools', '1', api.InvocationScope('i'), citations=api.CitationPolicyV1('required'))


def test_result_contract_descriptor_is_explicit_and_legacy_omission_is_exact():
    contract = api.ToolResultContractV1('1')
    tool = api.CustomTool('lookup', 'Lookup', SCHEMA, lambda a: {}, '1', result_contract=contract)
    assert tool.descriptor['result_contract'] == contract.to_dict()
    legacy = api.CustomTool('lookup', 'Lookup', SCHEMA, lambda a: {}, '1')
    assert set(legacy.descriptor) == {'name', 'description', 'parameters', 'version',
        'max_argument_bytes', 'max_result_bytes', 'authorization_required'}
