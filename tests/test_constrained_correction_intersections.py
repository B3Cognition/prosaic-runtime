"""Constrained correction where it meets existing features, on both wires where the feature applies."""

from dataclasses import replace
import json

import pytest

import prosaic_runtime as api
from prosaic_runtime import CustomTool, TurnAdmissionError, TurnLimits, Verdict
from support.context_counter import fixture_context
from support.conversation_server import ConversationFixture
from support.constrained_fixtures import VALID, WIRES, Tools, calls, constraint, runtime, scripted, text
from support.rules_support import recorder, tagged
from support.constrained_golden import _RANDOM_ID
from test_tool_evidence import bindings

MESSAGE = 'Report the synthetic gauge reading.'
KEY = 'tool_request_correction_constraint'
BAD_FIND = ('find_station', '{"query":"North Pier"}')
BAD_READ = ('read_gauge', json.dumps({**VALID['read_gauge'], 'unit': 'leagues'}))


def resend(name):
    return {'tool': name, 'arguments': VALID[name]}


def policy(fx, requests=3, tool_calls=1, **changes):
    return replace(fx.policy, max_provider_requests=requests, max_tool_calls=tool_calls, **changes)


def failure(turn):
    return turn.evidence.to_dict()['failureCode']


def normalized(value):
    """JSON text with per-invocation random call identifiers replaced by their order of first appearance."""
    ids = {}
    return _RANDOM_ID.sub(lambda m: f'<id{ids.setdefault(m.group(), len(ids))}>', json.dumps(value, sort_keys=True))


def later_turn(fx, malformed, name):
    """A valid read first, then a malformed call, a correction and the answer: four requests."""
    fx.responses.append(calls(('ok', 'find_station', json.dumps(VALID['find_station']))))
    scripted(fx, malformed, resend(name))


# initial_tool: forced on turn 1 only.


@pytest.mark.parametrize('wire', WIRES)
def test_a_forced_first_tool_with_bad_arguments_is_constrained_to_that_tool(wire):
    with ConversationFixture('openai-compatible') as fx:
        tools = Tools(fx)
        scripted(fx, BAD_READ, resend('read_gauge'))
        turn = runtime(fx, wire).turn(fx.agent, message=MESSAGE, policy=policy(fx, initial_tool='read_gauge'))
        first, correction, answer = fx.requests
    assert turn.result.exit_code == 0 and tools.reads == [('read_gauge', VALID['read_gauge'])]
    assert first['tool_choice'] == {'type': 'function', 'function': {'name': 'read_gauge'}}
    assert correction['tool_choice'] == 'none' and constraint(correction)['properties']['tool'] == {
        'const': 'read_gauge'
    }
    assert constraint(answer) is None and answer['tool_choice'] == 'auto'


@pytest.mark.parametrize('wire', [None, *WIRES])
def test_a_forced_first_tool_answered_with_an_ungranted_name_fails_before_any_correction(wire):
    with ConversationFixture('openai-compatible') as fx:
        tools = Tools(fx)
        scripted(fx, ('read_gauges', json.dumps(VALID['read_gauge'])), resend('read_gauge'))
        turn = runtime(fx, wire).turn(fx.agent, message=MESSAGE, policy=policy(fx, initial_tool='read_gauge'))
        assert len(fx.requests) == 1
    assert failure(turn) == 'providerFailure' and turn.tool_request_corrections == () and tools.reads == []
    assert KEY not in turn.result.metadata


@pytest.mark.parametrize('wire', WIRES)
@pytest.mark.parametrize('initial_tool', [None, 'find_station'])
def test_a_correction_on_a_later_turn_is_unforced_and_constrains_the_refused_tool(wire, initial_tool):
    with ConversationFixture('openai-compatible') as fx:
        tools = Tools(fx)
        later_turn(fx, BAD_READ, 'read_gauge')
        turn = runtime(fx, wire).turn(fx.agent, message=MESSAGE, policy=policy(fx, 4, 2, initial_tool=initial_tool))
        first, second, correction, answer = fx.requests
    assert turn.result.exit_code == 0
    assert tools.reads == [('find_station', VALID['find_station']), ('read_gauge', VALID['read_gauge'])]
    assert constraint(first) is None and constraint(second) is None and constraint(answer) is None
    tool_branch, answer_branch = constraint(correction)['anyOf']  # Unforced on a later turn: the answer is offered.
    assert correction['tool_choice'] == 'none' and tool_branch['properties']['tool'] == {'const': 'read_gauge'}
    assert answer_branch['properties'] == {'answer': {'type': 'string'}}
    assert second['tool_choice'] == answer['tool_choice'] == 'auto'
    assert correction['messages'][-3]['role'] == 'tool'  # The first read's result precedes the placeholder.


# tools_disabled, finalize_after_tool_rounds, terminal tools.


@pytest.mark.parametrize('wire', WIRES)
def test_finalization_after_a_corrected_read_removes_tools_and_the_constraint(wire):
    with ConversationFixture('openai-compatible') as fx:
        tools = Tools(fx)
        # The last message before the answer is the finalization instruction, not the tool result.
        scripted(fx, BAD_FIND, resend('find_station'), answer='Station ST-104.')
        turn = runtime(fx, wire, finalize_after_tool_rounds=1).turn(fx.agent, message=MESSAGE, policy=policy(fx))
        _, correction, answer = fx.requests
    assert turn.result.exit_code == 0 and len(tools.reads) == 1
    assert constraint(correction) is not None
    assert constraint(answer) is None and 'tools' not in answer and 'tool_choice' not in answer


@pytest.mark.parametrize('wire', [None, *WIRES])
def test_a_malformed_call_in_the_final_phase_is_never_corrected(wire):
    with ConversationFixture('openai-compatible') as fx:
        tools = Tools(fx)
        fx.responses.extend(
            [
                calls(('ok', 'find_station', json.dumps(VALID['find_station']))),
                calls(('late', *BAD_FIND)),
                text('unused'),
            ]
        )
        turn = runtime(fx, wire, finalize_after_tool_rounds=1).turn(fx.agent, message=MESSAGE, policy=policy(fx, 3, 2))
        assert len(fx.requests) == 2
    assert turn.result.exit_code == 1 and turn.tool_request_corrections == () and len(tools.reads) == 1
    assert KEY not in turn.result.metadata


@pytest.mark.parametrize('wire', WIRES)
def test_terminal_tools_stay_refused_at_admission(wire):
    with ConversationFixture('openai-compatible') as fx:
        fx.tools['finish'] = CustomTool(
            'finish', 'Terminal', {'type': 'object', 'additionalProperties': False}, lambda _: None, 'v1', terminal=True
        )
        with pytest.raises(TurnAdmissionError):
            runtime(fx, wire).turn(fx.agent, message=MESSAGE, policy=fx.policy)
        assert not fx.requests


# Output controls: json_mode with a forced first tool, and TOON tool results.


def test_json_mode_with_a_forced_first_tool_is_restored_after_the_correction():
    with ConversationFixture('openai-compatible') as fx:
        Tools(fx)
        scripted(fx, BAD_READ, resend('read_gauge'))
        runtime(fx, 'json_schema', json_mode=True).turn(
            fx.agent, message=MESSAGE, policy=policy(fx, initial_tool='read_gauge')
        )
        first, correction, answer = fx.requests
    assert first['response_format'] == answer['response_format'] == {'type': 'json_object'}
    assert first['tool_choice'] == {'type': 'function', 'function': {'name': 'read_gauge'}}
    assert correction['response_format']['type'] == 'json_schema' and correction['tool_choice'] == 'none'


def tool_messages(request):
    return [message for message in request['messages'] if message['role'] == 'tool']


@pytest.mark.parametrize('wire', WIRES)
def test_toon_tool_results_reach_the_correction_and_the_answer_unchanged(wire):
    sent = {}
    for arm in (None, wire):
        with ConversationFixture('openai-compatible') as fx:
            Tools(fx)
            fx.responses.append(calls(('ok', 'find_station', json.dumps(VALID['find_station']))))
            name, arguments = BAD_READ
            fx.responses.append(calls(('first', name, arguments)))
            fx.responses.append(
                lambda request: text(json.dumps(resend('read_gauge')))
                if constraint(request)
                else calls(('again', 'read_gauge', json.dumps(VALID['read_gauge'])))
            )
            fx.responses.append(text('Level 3.42 m.'))
            turn = runtime(fx, arm, tool_result_format='toon').turn(fx.agent, message=MESSAGE, policy=policy(fx, 4, 2))
            assert turn.result.exit_code == 0
            sent[arm] = [tool_messages(request) for request in fx.requests]
    assert normalized(sent[wire]) == normalized(sent[None])
    toon = sent[wire][2][0]['content']
    assert not toon.lstrip().startswith('{') and 'ST-104' in toon  # TOON, not JSON, on the correction request.


# Compaction and context packs.


@pytest.mark.parametrize('wire', WIRES)
def test_compaction_settings_do_not_touch_a_corrected_turn(wire):
    sent = {}
    for features in (
        {},
        {
            'compact_after_tool_results': 1,
            'keep_recent_tool_results': 1,
            'compact_tool_result_after_chars': 1,
            'compacted_result_chars': 1,
        },
    ):
        with ConversationFixture('openai-compatible') as fx:
            Tools(fx)
            later_turn(fx, BAD_READ, 'read_gauge')
            turn = runtime(fx, wire, **features).turn(fx.agent, message=MESSAGE, policy=policy(fx, 4, 2))
            assert turn.result.exit_code == 0
            sent[bool(features)] = [request['messages'] for request in fx.requests]
    assert normalized(sent[True]) == normalized(sent[False])


@pytest.mark.parametrize('wire', WIRES)
def test_a_context_pack_turn_counts_and_admits_the_constrained_request(wire):
    context_policy, counters = fixture_context(capacity=65_536)
    with ConversationFixture('openai-compatible') as fx:
        tools = Tools(fx)
        scripted(fx, BAD_FIND, resend('find_station'))
        turn = runtime(fx, wire).turn(
            fx.agent, message=MESSAGE, policy=policy(fx), context_policy=context_policy, token_counters=counters
        )
        assert constraint(fx.requests[1]) is not None
    assert turn.result.exit_code == 0 and len(tools.reads) == 1 and turn.result.metadata[KEY]['wire'] == wire


@pytest.mark.parametrize('wire', WIRES)
def test_a_constrained_request_over_the_context_capacity_fails_as_a_context_limit_with_evidence(wire):
    # The counter charges only the constrained request beyond capacity: it is refused before dispatch.
    context_policy, counters = fixture_context(count=lambda request: 10_000 if constraint(request) else 10)
    with ConversationFixture('openai-compatible') as fx:
        tools = Tools(fx)
        scripted(fx, BAD_READ, resend('read_gauge'))
        turn = runtime(fx, wire).turn(
            fx.agent, message=MESSAGE, policy=policy(fx), context_policy=context_policy, token_counters=counters
        )
        assert len(fx.requests) == 1
    assert failure(turn) == 'contextLimit' and tools.reads == []
    assert turn.result.metadata[KEY]['wire'] == wire


# Parallel tool calls.


def parallel_lookup(fx, reads):
    schema = {
        'type': 'object',
        'properties': {'i': {'type': 'integer'}},
        'required': ['i'],
        'additionalProperties': False,
    }
    fx.tools['lookup'] = CustomTool(
        'lookup',
        'Read',
        schema,
        lambda a, c: reads.append(a) or {'i': a['i']},
        '1',
        with_context=True,
        parallel_read=True,
    )
    return replace(fx.policy, parallel_tools=api.ParallelToolPolicyV1(max_concurrency=2), max_provider_requests=3)


@pytest.mark.parametrize('wire', WIRES)
def test_parallel_tools_with_one_refused_call_take_the_constrained_resend(wire):
    with ConversationFixture('openai-compatible') as fx:
        reads = []
        turn_policy = parallel_lookup(fx, reads)
        fx.responses.append(calls(('a', 'lookup', '{"i":"one"}')))
        fx.responses.append(
            lambda request: text('{"tool":"lookup","arguments":{"i":1}}')
            if constraint(request)
            else calls(('b', 'lookup', '{"i":"one"}'))
        )
        fx.responses.append(text('Value 1.'))
        turn = runtime(fx, wire).turn(fx.agent, message=MESSAGE, policy=turn_policy, tool_contract_bindings=bindings())
    assert turn.result.exit_code == 0 and reads == [{'i': 1}]
    assert turn.result.metadata[KEY]['scope'] == 'arguments'


@pytest.mark.parametrize('wire', WIRES)
def test_parallel_tools_with_a_refused_multi_call_batch_fall_back_to_ordinary_correction(wire):
    with ConversationFixture('openai-compatible') as fx:
        reads = []
        turn_policy = parallel_lookup(fx, reads)
        fx.responses.append(calls(('a', 'lookup', '{"i":0}'), ('b', 'lookup', '{"i":"one"}')))
        fx.responses.append(calls(('c', 'lookup', '{"i":0}'), ('d', 'lookup', '{"i":1}')))
        fx.responses.append(text('Values 0 and 1.'))
        turn = runtime(fx, wire).turn(fx.agent, message=MESSAGE, policy=turn_policy, tool_contract_bindings=bindings())
        assert all(constraint(request) is None for request in fx.requests)
    assert turn.result.exit_code == 0 and sorted(r['i'] for r in reads) == [0, 1]
    assert turn.result.metadata[KEY] == {'version': 1, 'wire': wire, 'issue': 'argumentsSchema', 'fallback': 'batch'}


# Prepared and frozen turns.


@pytest.mark.parametrize('wire', WIRES)
def test_a_prepared_turn_executes_under_the_same_value_and_is_refused_under_another(wire):
    other = WIRES[1 - WIRES.index(wire)]
    with ConversationFixture('openai-compatible') as fx:
        tools = Tools(fx)
        prepared = runtime(fx, wire).prepare_turn(fx.agent, message=MESSAGE, policy=policy(fx))
        for changed in (runtime(fx), runtime(fx, other)):
            with pytest.raises(TurnAdmissionError, match='bindingMismatch'):
                changed.turn(prepared)
        assert not fx.requests
        scripted(fx, BAD_FIND, resend('find_station'))
        turn = runtime(fx, wire).turn(prepared)
    assert turn.result.exit_code == 0 and len(tools.reads) == 1 and turn.result.metadata[KEY]['wire'] == wire


@pytest.mark.parametrize('wire', WIRES)
def test_a_frozen_turn_restores_only_under_the_same_value_and_then_corrects(wire):
    other = WIRES[1 - WIRES.index(wire)]
    with ConversationFixture('openai-compatible') as fx:
        tools = Tools(fx)
        turn_policy = policy(fx)
        prepared = runtime(fx, wire).prepare_turn(
            fx.agent, message=MESSAGE, input_data={'selected': 'x'}, input_profile='pack-v1', policy=turn_policy
        )
        frozen = prepared.export_frozen_input()
        for changed in (runtime(fx), runtime(fx, other)):
            with pytest.raises(TurnAdmissionError):
                changed.restore_turn(fx.agent, frozen_input=frozen, policy=turn_policy, limits=TurnLimits())
        restored = runtime(fx, wire).restore_turn(
            fx.agent, frozen_input=frozen, policy=turn_policy, limits=TurnLimits()
        )
        assert restored._encoded == prepared._encoded
        scripted(fx, BAD_FIND, resend('find_station'))
        turn = runtime(fx, wire).turn(restored)
    assert turn.result.exit_code == 0 and len(tools.reads) == 1


# Cancellation and budget.


@pytest.mark.parametrize('wire', WIRES)
def test_cancellation_during_the_correction_request_records_cancelled_with_no_read(wire):
    cancelled = []
    with ConversationFixture('openai-compatible') as fx:
        tools = Tools(fx)
        fx.responses.append(calls(('first', *BAD_FIND)))
        fx.responses.append(lambda request: cancelled.append(True) or text(json.dumps(resend('find_station'))))
        turn = runtime(fx, wire).turn(fx.agent, message=MESSAGE, policy=policy(fx), cancelled=lambda: bool(cancelled))
        assert len(fx.requests) == 2
    assert failure(turn) == 'cancelled' and tools.reads == [] and tools.authorized == []
    assert turn.result.metadata[KEY]['wire'] == wire


def outcome(turn, tools, fx):
    return failure(turn), len(tools.reads), len(fx.requests), turn.tool_request_corrections


@pytest.mark.parametrize('wire', WIRES)
def test_a_cap_of_two_behaves_exactly_as_ordinary_correction(wire):
    seen = {}
    for arm in (None, wire):
        with ConversationFixture('openai-compatible') as fx:
            tools = Tools(fx)
            fx.responses.append(calls(('first', *BAD_FIND)))
            fx.responses.append(
                lambda request: text(json.dumps(resend('find_station')))
                if constraint(request)
                else calls(('again', 'find_station', json.dumps(VALID['find_station'])))
            )
            fx.responses.append(text('unused'))
            turn = runtime(fx, arm).turn(fx.agent, message=MESSAGE, policy=policy(fx, 2))
            seen[arm] = outcome(turn, tools, fx)
    assert seen[wire] == seen[None]


@pytest.mark.parametrize('wire', WIRES)
def test_a_final_rule_retry_after_a_constrained_correction_spends_its_own_allowance(wire):
    rule = '- Report the level exactly. <!-- enforce: level_check(stage=final, on_violation=retry) -->\n'
    guard = recorder('level_check', Verdict(False), Verdict(True))
    with ConversationFixture('openai-compatible') as fx:
        tools = Tools(fx)
        scripted(fx, BAD_READ, resend('read_gauge'), answer='Level unknown.')
        fx.queue_text('Level 3.42 m.')
        turn = runtime(fx, wire, guards=[guard.guard]).turn(
            tagged(rule, tools=fx.tools), message=MESSAGE, policy=policy(fx, 4)
        )
        assert len(fx.requests) == 4
    assert turn.result.stdout == 'Level 3.42 m.' and len(tools.reads) == 1
    assert turn.tool_request_corrections == ('argumentsSchema',)
    assert turn.rules_retried == ('subagents/shelf-keeper.md:1:level_check',)
    assert turn.result.metadata[KEY]['wire'] == wire and turn.evidence.to_dict()['providerAttempts'] == 4


@pytest.mark.parametrize('wire', WIRES)
def test_a_before_tool_guard_refusing_the_host_made_call_stops_the_turn_with_evidence(wire):
    rule = '- Read only approved gauges. <!-- enforce: gauge_check(stage=before_tool, on_violation=fail) -->\n'
    guard = recorder('gauge_check', Verdict(False), stages=('before_tool',))
    with ConversationFixture('openai-compatible') as fx:
        tools = Tools(fx)
        scripted(fx, BAD_READ, resend('read_gauge'))
        turn = runtime(fx, wire, guards=[guard.guard]).turn(
            tagged(rule, tools=fx.tools), message=MESSAGE, policy=policy(fx)
        )
        assert len(fx.requests) == 2
    assert failure(turn) == 'ruleViolation' and tools.reads == [] and tools.authorized == []
    # The guard saw the host-made call, with the constrained arguments.
    stage, context = guard.calls[0]
    assert stage == 'before_tool' and context.tool_name == 'read_gauge' and context.arguments == VALID['read_gauge']
    assert turn.result.metadata[KEY]['wire'] == wire and turn.tool_request_corrections == ('argumentsSchema',)
