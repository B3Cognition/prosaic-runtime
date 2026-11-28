"""Constrained vs ordinary correction on the real wire: paired recovery, negative cases and default bytes."""

from dataclasses import replace
import json

from jsonschema import Draft202012Validator
import pytest

from prosaic_runtime import ProsaicRuntime, TurnAdmissionError
from support.conversation_server import ConversationFixture
from support.constrained_fixtures import (
    FEATURE,
    RESULTS,
    SCHEMAS,
    VALID,
    WIRES,
    Tools,
    calls,
    constraint,
    runtime,
    scripted,
    text,
)
from support import constrained_golden

MESSAGE = 'Report the synthetic gauge reading.'
KEY = 'tool_request_correction_constraint'


def resend(name):
    return {'tool': name, 'arguments': VALID[name]}


# Paired fixtures. Each malformed call is a different failure a weak model makes; the resend is what a
# constrained decoder emits. Required string, integer, enum and nested-object arguments are all covered.
CASES = {
    'missing_required_integer': (('find_station', '{"query":"North Pier"}'), 'find_station', 'argumentsSchema'),
    'string_sent_as_integer': (('find_station', '{"query":7,"limit":1}'), 'find_station', 'argumentsSchema'),
    'enum_outside_values': (
        ('read_gauge', json.dumps({**VALID['read_gauge'], 'unit': 'leagues'})),
        'read_gauge',
        'argumentsSchema',
    ),
    'nested_object_incomplete': (
        ('read_gauge', json.dumps({**VALID['read_gauge'], 'window': {'start': '2026-01-05'}})),
        'read_gauge',
        'argumentsSchema',
    ),
    'ungranted_name_for_read': (('read_gauges', json.dumps(VALID['read_gauge'])), 'read_gauge', 'toolNotGranted'),
    'ungranted_name_for_find': (('find_stations', json.dumps(VALID['find_station'])), 'find_station', 'toolNotGranted'),
}


def policy(fx, **changes):
    return replace(fx.policy, max_provider_requests=3, max_tool_calls=1, **changes)


def recovered(fx, tools, turn, name):
    """Schema-valid arguments, exactly one authorised read, a final answer grounded in it, in three requests."""
    if turn.result.exit_code != 0 or len(fx.requests) != 3:
        return False
    if tools.reads != [(name, VALID[name])] or tools.authorized != tools.reads:
        return False
    if not Draft202012Validator(SCHEMAS[name]).is_valid(tools.reads[0][1]):
        return False
    return json.dumps(RESULTS[name], sort_keys=True) in turn.result.stdout


@pytest.mark.parametrize('case', sorted(CASES))
@pytest.mark.parametrize('wire', WIRES)
def test_constrained_correction_recovers(wire, case):
    malformed, name, issue = CASES[case]
    with ConversationFixture('openai-compatible') as fx:
        tools = Tools(fx)
        scripted(fx, malformed, resend(name))
        turn = runtime(fx, wire).turn(fx.agent, message=MESSAGE, policy=policy(fx))
        assert recovered(fx, tools, turn, name)
        first, correction, answer = fx.requests
    assert constraint(correction) is not None and correction['tool_choice'] == 'none'
    assert constraint(first) is None and constraint(answer) is None
    assert first['tool_choice'] == answer['tool_choice'] == 'auto'
    assert turn.tool_request_corrections == (issue,)
    record = turn.result.metadata[KEY]
    assert record['wire'] == wire and record['issue'] == issue and len(record['schemaSha256']) == 64
    assert turn.evidence.to_dict()['providerAttempts'] == 3


@pytest.mark.parametrize('case', sorted(CASES))
def test_ordinary_correction_does_not_recover_the_same_weak_model(case):
    malformed, name, _ = CASES[case]
    with ConversationFixture('openai-compatible') as fx:
        tools = Tools(fx)
        scripted(fx, malformed, resend(name))
        turn = runtime(fx).turn(fx.agent, message=MESSAGE, policy=policy(fx))
        assert not recovered(fx, tools, turn, name)
        assert all(constraint(request) is None for request in fx.requests)
    assert turn.evidence.to_dict()['failureCode'] == 'invalidToolRequest'
    assert tools.reads == [] and KEY not in turn.result.metadata


@pytest.mark.parametrize('wire', WIRES)
def test_the_correction_request_keeps_the_ordinary_envelope_and_placeholder(wire):
    with ConversationFixture('openai-compatible') as fx:
        Tools(fx)
        scripted(fx, CASES['missing_required_integer'][0], resend('find_station'))
        runtime(fx, wire).turn(fx.agent, message=MESSAGE, policy=policy(fx))
    with ConversationFixture('openai-compatible') as control:
        Tools(control)
        scripted(control, CASES['missing_required_integer'][0], resend('find_station'))
        runtime(control).turn(control.agent, message=MESSAGE, policy=policy(control))
    assert fx.requests[1]['messages'] == control.requests[1]['messages']


def test_json_mode_returns_after_the_correction_request():
    with ConversationFixture('openai-compatible') as fx:
        Tools(fx)
        scripted(fx, CASES['enum_outside_values'][0], resend('read_gauge'))
        runtime(fx, 'json_schema', json_mode=True).turn(fx.agent, message=MESSAGE, policy=policy(fx))
    first, correction, answer = fx.requests
    assert first['response_format'] == answer['response_format'] == {'type': 'json_object'}
    assert correction['response_format']['type'] == 'json_schema'
    assert correction['response_format']['json_schema']['strict'] is True


def test_a_required_first_tool_stays_the_only_choice_and_is_checked():
    with ConversationFixture('openai-compatible') as fx:
        tools = Tools(fx)
        scripted(fx, CASES['nested_object_incomplete'][0], resend('read_gauge'))
        turn = runtime(fx, 'json_schema').turn(fx.agent, message=MESSAGE, policy=policy(fx, initial_tool='read_gauge'))
        assert recovered(fx, tools, turn, 'read_gauge')
    first, correction, _ = fx.requests
    assert first['tool_choice'] == {'type': 'function', 'function': {'name': 'read_gauge'}}
    assert correction['tool_choice'] == 'none'
    assert constraint(correction)['properties']['tool'] == {'const': 'read_gauge'}


# Negative cases.


@pytest.mark.parametrize(
    'response',
    [
        text('Gauge reads 3.42 m.'),  # Ignored the constraint: an ungrounded answer.
        calls(('native', 'find_station', json.dumps(VALID['find_station']))),  # A native call, even a valid one.
        text('{"tool":"find_station","arguments":{"query":"North Pier"}}'),  # Content outside the schema.
        text('{"tool": "find_station", "arguments": {'),  # Truncated content.
    ],
)
@pytest.mark.parametrize('wire', WIRES)
def test_a_provider_that_ignores_the_constraint_fails_as_today(wire, response):
    with ConversationFixture('openai-compatible') as fx:
        tools = Tools(fx)
        fx.responses.extend(
            [calls(('first', 'find_station', '{"query":"North Pier"}')), response, text('Should not be reached')]
        )
        turn = runtime(fx, wire).turn(fx.agent, message=MESSAGE, policy=policy(fx))
        assert len(fx.requests) == 2
    # The existing unhonoured-choice failure (tool_choice_not_honored on the wire), as for a forced first tool.
    assert turn.result.exit_code == 1 and turn.completed_turn is None
    assert turn.evidence.to_dict()['failureCode'] == turn.result.metadata['failure_reason'] == 'providerFailure'
    assert tools.reads == [] and tools.authorized == []
    assert turn.result.metadata[KEY]['wire'] == wire


@pytest.mark.parametrize('wire', WIRES)
def test_a_schema_valid_but_unauthorised_call_is_refused_by_the_existing_check(wire):
    with ConversationFixture('openai-compatible') as fx:
        tools = Tools(fx, authorize=False)
        scripted(fx, CASES['ungranted_name_for_read'][0], resend('read_gauge'), answer='Not permitted.')
        turn = runtime(fx, wire).turn(fx.agent, message=MESSAGE, policy=policy(fx))
        denied = json.loads(fx.requests[2]['messages'][-1]['content'])
    assert tools.authorized == [('read_gauge', VALID['read_gauge'])] and tools.reads == []
    assert denied['status'] == 'error'
    assert turn.result.stdout == 'Not permitted.'


def test_a_resend_that_breaks_a_dropped_assertion_is_refused_by_batch_admission():
    with ConversationFixture('openai-compatible') as fx:
        tools = Tools(fx)
        too_many = {'tool': 'find_station', 'arguments': {'query': 'North Pier', 'limit': 9}}  # maximum is 5.
        scripted(fx, CASES['missing_required_integer'][0], too_many)
        turn = runtime(fx, 'json_schema').turn(fx.agent, message=MESSAGE, policy=policy(fx))
        assert len(fx.requests) == 2
    assert turn.evidence.to_dict()['failureCode'] == 'invalidToolRequest'
    assert tools.reads == [] and tools.authorized == []
    assert 'maximum' in turn.result.metadata[KEY]['droppedKeywords']


def test_an_oversize_schema_falls_back_to_ordinary_correction_and_records_it():
    values = [f'synthetic-code-{index:05d}-padding' for index in range(700)]
    big = {
        'type': 'object',
        'additionalProperties': False,
        'required': ['code'],
        'properties': {'code': {'type': 'string', 'enum': values}},
    }
    with ConversationFixture('openai-compatible') as fx:
        tools = Tools(fx, names=('find_station',), schemas={'find_station': big})
        fx.responses.extend(
            [
                calls(('first', 'find_station', '{"code":"none"}')),
                calls(('again', 'find_station', json.dumps({'code': values[3]}))),
                text('Found it.'),
            ]
        )
        turn = runtime(fx, 'json_schema').turn(fx.agent, message=MESSAGE, policy=policy(fx))
        assert all(constraint(request) is None for request in fx.requests)
        assert fx.requests[1]['tool_choice'] == 'auto'
    assert turn.result.exit_code == 0 and tools.reads == [('find_station', {'code': values[3]})]
    assert turn.result.metadata[KEY] == {
        'version': 1,
        'wire': 'json_schema',
        'issue': 'argumentsSchema',
        'fallback': 'oversize',
    }


def test_no_correction_after_tools_are_disabled_for_lack_of_progress():
    with ConversationFixture('openai-compatible') as fx:
        fx.add_tool('lookup', lambda args: {'tide': 'high'})
        for index in range(3):
            fx.responses.append(calls((f'r{index}', 'lookup', '{}')))
        fx.responses.extend([calls(('m', 'lookup', '{"shelf":"QA-999"}')), text('Should not be reached')])
        turn = runtime(fx, 'json_schema').turn(fx.agent, message=MESSAGE, policy=fx.policy)
        assert len(fx.requests) == 4 and all(constraint(request) is None for request in fx.requests)
    assert turn.evidence.to_dict()['failureCode'] == 'invalidToolRequest'
    assert turn.tool_request_corrections == () and KEY not in turn.result.metadata


def test_no_correction_without_budget_and_none_without_the_correction_switch():
    with ConversationFixture('openai-compatible') as fx:
        Tools(fx)
        scripted(fx, CASES['missing_required_integer'][0], resend('find_station'))
        turn = runtime(fx, 'json_schema').turn(
            fx.agent, message=MESSAGE, policy=replace(fx.policy, max_provider_requests=1)
        )
        assert len(fx.requests) == 1 and KEY not in turn.result.metadata
    with ConversationFixture('openai-compatible') as fx:
        Tools(fx)
        scripted(fx, CASES['missing_required_integer'][0], resend('find_station'))
        switched_off = ProsaicRuntime(runtime(fx, 'json_schema').config, custom_tools=fx.tools)
        turn = switched_off.turn(fx.agent, message=MESSAGE, policy=policy(fx))
        assert len(fx.requests) == 1 and KEY not in turn.result.metadata
    assert turn.evidence.to_dict()['failureCode'] == 'invalidToolRequest'


# Identity: absent opt-in is byte-identical to main; present opt-in is sealed in the profile identity.

MAIN_5176BDD = {
    'requests': [
        '38ec3a2e3b8f8475fbc8cc430ba5f75c02e8b3be4ccf089b39a70e7109b9e888',
        '973ddb93dc416a936486523cf636462fe1286a23ee19642eec83b0449068a62c',
        'fa81aea928967e71ff59d83da17e30ee8204c94920699eb3349105c11a8323d6',
    ],
    'prepared': 'bebe2dce34698c20d03d843827f82d33474ae4137b4f9702ffbcc0bf95b48200',
    'identity': 'cfbb99d4eb28eae19c2bf590c3e6b448f441e556206498c1319c00752b098397',
    'evidence': '635536120fd6902e147a23ea8715b49cc959e4c0655324aa641dadc2c03607f1',
    'completed': '247a71fdb0e0c10e18f628c0cfbe5e5fbd98549c5d9bf32b2e854fd7752f0789',
    'metadata': '0766c8d6a439cad91faadd24846d29c4c21fb78c0b2bc559448ab22900da4bb1',
    'exit': 0,
    'corrections': ['argumentsSchema'],
}


def test_with_the_opt_in_absent_a_turn_is_byte_identical_to_main():
    assert constrained_golden.observe() == MAIN_5176BDD


def test_the_opt_in_is_sealed_in_the_prepared_profile_identity():
    with ConversationFixture('openai-compatible') as fx:
        Tools(fx)
        prepared = runtime(fx, 'json_schema').prepare_turn(fx.agent, message=MESSAGE, policy=policy(fx))
        other = runtime(fx, 'omlx_grammar').prepare_turn(fx.agent, message=MESSAGE, policy=policy(fx))
        assert prepared.context_identity != other.context_identity
        for changed in (runtime(fx), runtime(fx, 'omlx_grammar')):
            with pytest.raises(TurnAdmissionError, match='bindingMismatch'):
                changed.turn(prepared)
        assert not fx.requests


def test_the_nemotron_correction_is_unchanged_by_the_general_opt_in():
    from prosaic_runtime.tool_request_correction import constrain_nemotron_lookup
    from prosaic_runtime.tool_registry import InvalidToolRequest

    payload = {
        'tools': [{'function': {'name': 'lookup', 'parameters': {'type': 'object', 'additionalProperties': False}}}]
    }
    error = InvalidToolRequest('argumentsSchema', 0, 'lookup')
    assert constrain_nemotron_lookup(dict(payload), {FEATURE: 'json_schema'}, error) is None
