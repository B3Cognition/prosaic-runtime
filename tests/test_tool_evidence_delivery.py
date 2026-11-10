import json
import hashlib
import pytest
import prosaic_runtime as api
from support.conversation_server import ConversationFixture
from support.rules_support import runtime_for, tagged
from test_tool_evidence import bindings


def configure(fx, calls, grant=lambda use: True):
    def handler(a):
        calls.append(1)
        return api.ToolResultV1(data={'count': 14}, evidence=(
            api.EvidenceDraftV1('graph', 'r1', 'PRIVATE-LOCATOR', '/count', 'Dependents'),))
    fx.tools['lookup'] = api.CustomTool('lookup', 'Lookup', {'type': 'object', 'additionalProperties': False},
        handler, '1', result_contract=api.ToolResultContractV1('1', {'type': 'object', 'required': ['count'],
            'properties': {'count': {'type': 'integer'}}}),
        event_policy=api.ToolEventPolicyV1('1', {'count': api.ToolDisplayFieldV1('result', '/count')}))
    fx.queue_calls([{'id': 'PROVIDER-PRIVATE-ID', 'name': 'lookup', 'arguments': '{}'}])
    fx.queue_text('Done')
    return bindings(grant)


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_only_submitted_value_and_reference_become_eligible(provider):
    with ConversationFixture(provider) as fx:
        calls = []
        binding = configure(fx, calls)
        result = fx.runtime.run(fx.agent, policy=fx.policy, tool_contract_bindings=binding)
        assert result.exit_code == 0 and calls == [1] and len(fx.requests) == 2
        wire = fx.raw_requests[1].decode()
        assert 'PRIVATE-LOCATOR' not in wire and 'sourceRevision' not in wire and 'ev_' in wire
        evidence = result.metadata['tool_contract_v1']
        record = evidence['records'][0]
        assert evidence['deliveries'][record['id']][0]['inputSha256'] == hashlib.sha256(fx.raw_requests[1]).hexdigest()


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_unsent_result_has_no_delivery(provider):
    with ConversationFixture(provider) as fx:
        calls = []
        binding = configure(fx, calls, lambda use: False)
        result = fx.runtime.run(fx.agent, policy=fx.policy, tool_contract_bindings=binding)
        assert result.exit_code != 0 and result.metadata['failure_reason'] == 'evidenceUseDenied'
        assert calls == [1] and len(fx.requests) == 1
        assert result.metadata['tool_contract_v1']['deliveries'] == {}
        assert len(result.metadata['tool_contract_v1']['calls']) == 1


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
@pytest.mark.parametrize('replacement', [15, 'invalid'])
def test_post_tool_fix_withdraws_changed_selection_and_rechecks_schema(provider, replacement):
    with ConversationFixture(provider) as fx:
        calls = []
        binding = configure(fx, calls)
        def fix(stage, context):
            payload = context.tool_result
            if payload['result']['data']['count'] != replacement:
                payload['result']['data']['count'] = replacement
                return api.Verdict(False, fixed=payload)
            return api.Verdict(True)
        guard = api.Guard('redact', '1', ('after_tool',), fix, fixes=True)
        agent = tagged('- ALWAYS use checked results. <!-- enforce: redact(stage=after_tool, on_violation=fix) -->', tools=['lookup'])
        result = runtime_for(fx, guards=[guard]).turn(agent, message='Review', policy=fx.policy,
            tool_contract_bindings=binding)
        if replacement == 'invalid':
            assert result.result.exit_code != 0 and len(fx.requests) == 1
        else:
            assert result.result.exit_code == 0 and len(fx.requests) == 2
            assert 'ev_' not in fx.raw_requests[1].decode() and '15' in fx.raw_requests[1].decode()
        assert calls == [1]
