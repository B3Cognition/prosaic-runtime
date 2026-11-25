"""Evidence delivery checks, receipts and citations read the request as sent, whatever its tool result format."""
from dataclasses import replace
import hashlib

import pytest
from toon_format import decode

import prosaic_runtime as api
from support.conversation_server import ConversationFixture
from test_citation_execution import dynamic_answer
from test_tool_evidence import bindings
from test_tool_evidence_delivery import configure
from test_toon_tool_results import tool_contents

PROVIDERS = pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
FORMATS = pytest.mark.parametrize('result_format', ['json', 'toon'])
COMPACT_AT_ONCE = {'compact_after_tool_results': 0, 'keep_recent_tool_results': 0,
                   'compact_tool_result_after_chars': 10}


def is_toon(content):
    return not content.lstrip().startswith('{')


@PROVIDERS
@FORMATS
def test_use_denied_after_staging_stops_before_the_result_is_sent(provider, result_format):
    with ConversationFixture(provider) as fx:
        fx.features = {'tool_result_format': result_format}
        uses = []

        def grant(use):
            uses.append(use.purpose)
            return len(uses) == 1  # allow staging, deny every later use

        binding = configure(fx, [], grant)
        result = fx.runtime.run(fx.agent, policy=fx.policy, tool_contract_bindings=binding)
        assert len(fx.requests) == 1
        assert result.metadata['failure_reason'] == 'evidenceUseDenied'
        assert result.metadata['tool_contract_v1']['deliveries'] == {}


@PROVIDERS
@FORMATS
def test_sent_result_has_a_receipt_for_the_request_bytes(provider, result_format):
    with ConversationFixture(provider) as fx:
        fx.features = {'tool_result_format': result_format}
        binding = configure(fx, [])
        result = fx.runtime.run(fx.agent, policy=fx.policy, tool_contract_bindings=binding)
        assert result.exit_code == 0
        [content] = tool_contents(fx.requests[1])
        assert is_toon(content) == (result_format == 'toon')
        evidence = result.metadata['tool_contract_v1']
        [record] = evidence['records']
        assert evidence['deliveries'][record['id']] == [
            {'requestId': evidence['deliveries'][record['id']][0]['requestId'],
             'inputSha256': hashlib.sha256(fx.raw_requests[1]).hexdigest()}]


@PROVIDERS
@FORMATS
def test_required_citation_of_a_sent_result_is_accepted(provider, result_format):
    with ConversationFixture(provider) as fx:
        fx.features = {'tool_result_format': result_format}
        binding = replace(configure(fx, []), citations=api.CitationPolicyV1('required'))
        dynamic_answer(fx)
        result = fx.runtime.run(fx.agent, policy=fx.policy, tool_contract_bindings=binding)
        assert result.exit_code == 0 and '[cite:ev_' in result.stdout
        assert api.CitationsV1.from_dict(result.metadata['citations_v1']).verify_answer(result.stdout)


@PROVIDERS
@FORMATS
def test_compacted_result_is_not_marked_delivered(provider, result_format, tmp_path):
    with ConversationFixture(provider) as fx:
        fx.features = {'tool_result_format': result_format, **COMPACT_AT_ONCE}
        binding = configure(fx, [])
        result = fx.runtime.run(fx.agent, cwd=tmp_path, policy=fx.policy, tool_contract_bindings=binding)
        assert result.exit_code == 0 and result.metadata['tool_result_compactions'] == 1
        assert 'ev_' not in fx.raw_requests[1].decode()
        assert len(result.metadata['tool_contract_v1']['records']) == 1
        assert result.metadata['tool_contract_v1']['deliveries'] == {}


@PROVIDERS
def test_whole_number_floats_still_match_the_sent_text(provider):
    """TOON writes 2.0 as 2; the receipt must still be found for the text the model was sent."""
    with ConversationFixture(provider) as fx:
        fx.features = {'tool_result_format': 'toon'}
        fx.tools['lookup'] = api.CustomTool('lookup', 'Lookup', {'type': 'object', 'additionalProperties': False},
            lambda a: api.ToolResultV1(data={'rows': [{'id': 1, 'score': 2.0}, {'id': 2, 'score': 0.5}]}, evidence=(
                api.EvidenceDraftV1('graph', 'r1', 'PRIVATE-LOCATOR', '/rows', 'Scores'),)), '1',
            result_contract=api.ToolResultContractV1('1', {'type': 'object'}))
        fx.queue_calls([{'id': 'call_1', 'name': 'lookup', 'arguments': '{}'}])
        fx.queue_text('Done')
        result = fx.runtime.run(fx.agent, policy=fx.policy, tool_contract_bindings=bindings(lambda use: True))
        assert result.exit_code == 0
        [content] = tool_contents(fx.requests[1])
        assert decode(content)['result']['data']['rows'][0]['score'] == 2
        evidence = result.metadata['tool_contract_v1']
        assert list(evidence['deliveries']) == [evidence['records'][0]['id']]


@PROVIDERS
@pytest.mark.parametrize('depth', [2, 70])
def test_toon_looking_user_text_remains_data(provider, depth):
    """Only tool results are read as TOON; user text that looks like it, however deep, is sent as typed."""
    text = 'status: ok\nrows[1]{' + 'k{' * depth + 'v' + '}' * depth + '}:\n  x'
    with ConversationFixture(provider) as fx:
        fx.queue_text('Done')  # the endpoint keeps its default JSON result format
        result = fx.runtime.turn(fx.agent, message=text, policy=fx.policy, tool_contract_bindings=bindings())
        assert result.result.exit_code == 0
        assert len(fx.requests) == 1
        assert result.result.stdout == 'Done'


@PROVIDERS
def test_deeply_nested_json_user_text_remains_data(provider):
    """Text past the digest bounds cannot match staged evidence, so it is sent rather than failing the turn."""
    text = '{"a": ' * 70 + '1' + '}' * 70
    with ConversationFixture(provider) as fx:
        fx.queue_text('Done')
        result = fx.runtime.turn(fx.agent, message=text, policy=fx.policy, tool_contract_bindings=bindings())
        assert result.result.exit_code == 0
        assert len(fx.requests) == 1
