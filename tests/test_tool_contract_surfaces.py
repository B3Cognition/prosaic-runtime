from dataclasses import replace
import json
import time
import pytest
import prosaic_runtime as api
from prosaic_runtime.structured_tools import StructuredToolLoop, ToolRequest
from test_direct_tool import runtime, POLICY
from test_tool_evidence import bindings


def typed_runtime():
    return runtime(lambda args: api.ToolResultV1(data={'count': 14}, evidence=(
        api.EvidenceDraftV1('graph', 'r1', 'PRIVATE-LOCATOR', '/count', 'Dependents'),)),
        result_contract=api.ToolResultContractV1('1'))


def test_direct_tool_has_zero_provider_exposure():
    result = typed_runtime().run_tool('lookup', {'value': 2}, policy=POLICY, tool_contract_bindings=bindings())
    assert result.exit_code == 0 and result.metadata['tool_contract_v1']['deliveries'] == {}
    assert result.metadata['direct_tool_v1']['provider_requests'] == 0
    assert 'PRIVATE-LOCATOR' not in result.stdout and json.loads(result.stdout)['data'] == {'count': 14}
    assert 'citations_v1' not in result.metadata


def test_bare_result_cannot_confirm_delivery():
    loop = StructuredToolLoop(typed_runtime()._custom_tools, arguments={}, deadline=time.monotonic() + 30,
        allowed_tools={'lookup'}, tool_contract_bindings=bindings())
    call, data = loop.dispatch(ToolRequest('lookup', {'value': 2}))
    assert 'sources' not in data and 'PRIVATE-LOCATOR' not in str(data)
    with pytest.raises(ValueError): loop.attest_delivery(api.Result(0, json.dumps(data), ''))
    proof = api.HostDeliveryAttestationV1(bindings().scope, 'host-request', {'messages': [
        {'status': 'ok', 'result': data}]})
    eligible = loop.attest_delivery(proof)
    assert eligible.proof == 'hostAttested' and eligible.ids == (data['evidence'][0]['id'],)
    assert loop.evidence.to_dict()['deliveries'] == {}
    citations = api.validate_citations(f'[cite:{eligible.ids[0]}]', eligible, api.CitationPolicyV1('required'))
    assert citations.to_dict()['proof'] == 'hostAttested'


@pytest.mark.parametrize('route', ['admit', 'tool_outcome'])
def test_structured_automatic_citations_refuse_before_host_callbacks(route):
    called = []
    with pytest.raises(ValueError, match='unsupported.*citation'):
        loop = StructuredToolLoop(typed_runtime()._custom_tools, arguments={}, deadline=time.monotonic() + 30,
            allowed_tools={'lookup'}, tool_contract_bindings=replace(bindings(), citations=api.CitationPolicyV1('required')))
        loop.turns(**{route: lambda *args: called.append(args)})
    assert called == []


def test_direct_automatic_citations_refuse_before_dispatch():
    calls = []
    rt = runtime(lambda args: calls.append(1))
    with pytest.raises(ValueError, match='unsupported.*citation'):
        rt.run_tool('lookup', {'value': 2}, policy=POLICY,
            tool_contract_bindings=replace(bindings(), citations=api.CitationPolicyV1('required')))
    assert calls == []


def test_typed_model_loop_requires_evidence_binding_before_effects():
    from support.conversation_server import ConversationFixture
    from test_tool_evidence_delivery import configure
    with ConversationFixture('openai-compatible') as fx:
        calls = []
        configure(fx, calls)
        with pytest.raises(ValueError): fx.runtime.run(fx.agent, policy=fx.policy)
        assert calls == [] and fx.requests == []


def test_terminal_tools_with_contracts_refuse_before_effects():
    rt = runtime(terminal=True)
    with pytest.raises(ValueError):
        rt.run_tool('lookup', {'value': 2}, policy=POLICY,
            tool_contract_bindings=replace(bindings(), citations=api.CitationPolicyV1('required')))
