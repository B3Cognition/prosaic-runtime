"""The answer branch: under a constrained correction an optional tool keeps the offer to answer without it."""

import json

import pytest

from prosaic_runtime import constrained_correction as cc
from support.constrained_fixtures import VALID, WIRES, Tools, calls, constraint, runtime, scripted, text
from support.conversation_server import ConversationFixture
from test_constrained_correction import TOOLS, plan, turn as provider_turn

MESSAGE = 'Report the synthetic gauge reading.'
KEY = 'tool_request_correction_constraint'
BAD_FIND = ('find_station', '{"query":"North Pier"}')


def policy(fx, requests=3):
    from dataclasses import replace

    return replace(fx.policy, max_provider_requests=requests, max_tool_calls=1)


def failure(turn):
    return turn.evidence.to_dict()['failureCode']


def test_a_required_first_tool_gets_no_answer_branch_for_either_issue():
    for issue, tool in (('argumentsSchema', 'read_gauge'), ('toolNotGranted', None)):
        result = plan(issue, tool, required_tool='read_gauge')
        assert 'anyOf' not in result.schema and result.record['answerBranch'] is False


def test_an_answer_reply_becomes_the_text_answer_with_no_call():
    candidate = provider_turn(json.dumps({'answer': 'No gauge fits; the level is unknown.'}))
    assert cc.accept(candidate, plan(), [])
    assert candidate.text == 'No gauge fits; the level is unknown.'
    assert candidate.tool_calls == [] and candidate.finish_reason == 'stop'


def test_an_answer_reply_is_refused_when_the_tool_is_required():
    candidate = provider_turn(json.dumps({'answer': 'Skipping the read.'}))
    assert not cc.accept(candidate, plan(required_tool='find_station'), [])
    assert candidate.tool_calls == [] and candidate.text == json.dumps({'answer': 'Skipping the read.'})


@pytest.mark.parametrize('content', ['{"answer": 7}', '{"answer": "x", "tool": "find_station"}', '{}'])
def test_a_malformed_answer_is_refused(content):
    assert not cc.accept(provider_turn(content), plan(), [])


def test_a_schema_valid_resend_may_name_another_granted_tool():
    # Intended: for toolNotGranted the constraint admits any granted tool; authorisation decides whether it runs.
    candidate = provider_turn(json.dumps({'tool': 'read_gauge', 'arguments': VALID['read_gauge']}))
    assert cc.accept(candidate, plan('toolNotGranted', None), [])
    assert candidate.tool_calls[0]['function']['name'] == 'read_gauge'


def test_a_tool_without_parameters_falls_back_instead_of_raising():
    tools = [{'type': 'function', 'function': {'name': 'probe', 'description': 'd'}}]
    assert plan(tool='probe', tools=tools).record['fallback'] == 'unsupported'
    assert plan('toolNotGranted', None, tools=[*TOOLS, *tools]).record['scope'] == 'name'


# On the wire.


@pytest.mark.parametrize('wire', WIRES)
def test_an_optional_tool_answered_in_text_under_a_constrained_correction_succeeds_without_a_read(wire):
    with ConversationFixture('openai-compatible') as fx:
        tools = Tools(fx)
        fx.responses.extend(
            [calls(('first', *BAD_FIND)), text('{"answer": "I cannot find that station."}'), text('unused')]
        )
        turn = runtime(fx, wire).turn(fx.agent, message=MESSAGE, policy=policy(fx))
        assert len(fx.requests) == 2
        branches = constraint(fx.requests[1])['anyOf']
    assert turn.result.exit_code == 0 and turn.result.stdout == 'I cannot find that station.'
    assert tools.reads == [] and tools.authorized == []
    assert branches[-1]['properties'] == {'answer': {'type': 'string'}}
    assert turn.result.metadata[KEY]['answerBranch'] is True
    # Only the answer text enters history, never the JSON envelope around it.
    completed = turn.completed_turn.to_dict()
    texts = [
        block['text']
        for message in completed['messages']
        if message['role'] == 'assistant'
        for block in message['content']
        if block['type'] == 'text'
    ]
    assert texts == ['I cannot find that station.']
    assert '{"answer' not in json.dumps(completed, ensure_ascii=False).replace('\\"', '"')


@pytest.mark.parametrize('wire', WIRES)
def test_a_required_first_tool_answered_in_text_fails_as_an_unhonoured_choice(wire):
    from dataclasses import replace

    with ConversationFixture('openai-compatible') as fx:
        tools = Tools(fx)
        fx.responses.extend([calls(('first', *BAD_FIND)), text('{"answer": "Skipping it."}'), text('unused')])
        turn = runtime(fx, wire).turn(
            fx.agent, message=MESSAGE, policy=replace(policy(fx), initial_tool='find_station')
        )
        assert 'anyOf' not in constraint(fx.requests[1])
    assert failure(turn) == 'providerFailure' and tools.reads == []
    assert turn.result.metadata[KEY]['answerBranch'] is False


@pytest.mark.parametrize('wire', WIRES)
def test_an_authorised_read_of_another_granted_tool_runs_as_the_constraint_allows(wire):
    with ConversationFixture('openai-compatible') as fx:
        tools = Tools(fx)
        scripted(
            fx,
            ('read_gauges', '{}'),
            {'tool': 'find_station', 'arguments': VALID['find_station']},
            answer='Station ST-104.',
        )
        turn = runtime(fx, wire).turn(fx.agent, message=MESSAGE, policy=policy(fx))
    assert turn.result.exit_code == 0 and tools.reads == [('find_station', VALID['find_station'])]


@pytest.mark.parametrize('status', [400, 422, 500])
@pytest.mark.parametrize('wire', WIRES)
def test_a_server_that_rejects_the_schema_fails_the_turn_after_two_requests_with_no_read(wire, status):
    with ConversationFixture('openai-compatible') as fx:
        tools = Tools(fx)
        fx.responses.append(calls(('first', *BAD_FIND)))
        fx.queue_http_failure(status)
        fx.queue_text('unused')
        turn = runtime(fx, wire).turn(fx.agent, message=MESSAGE, policy=policy(fx))
        assert len(fx.requests) == 2
    assert failure(turn) == 'providerFailure' and tools.reads == []
    assert turn.evidence.to_dict()['providerAttempts'] == 2
    assert turn.result.metadata[KEY]['wire'] == wire
