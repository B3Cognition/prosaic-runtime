"""Undeliverable feedback is a guard fault, caught before any further provider request."""

import json

import pytest

from prosaic_runtime import CustomTool, Verdict
from support.conversation_server import ConversationFixture
from support.rules_support import recorder, runtime_for, tagged

PROVIDERS = ['openai-compatible', 'anthropic']
RULE_ID = 'subagents/shelf-keeper.md:1:shelf_check'
TOOL_STAGES = ('before_tool', 'after_tool')
UNDELIVERABLE = {'huge-integer': 10**4500, 'lone-surrogate': 'QA-\ud800', 'surrogate-in-long-text': 'x' * 10 + '\udfff'}


def rule(stage):
    return f'- Quote shelf marks exactly. <!-- enforce: shelf_check(stage={stage}, on_violation=retry) -->\n'


def failure(turn):
    return turn.evidence.to_dict()['failureCode']


def tool_turn(fx, guard, stage):
    calls = []

    def lookup(arguments):
        calls.append(arguments)
        return {'shelfMark': 'QA-104'}

    fx.tools['shelf_lookup'] = CustomTool(
        'shelf_lookup',
        'Look up a synthetic shelf mark',
        {'type': 'object', 'properties': {'recordId': {'type': 'string'}}, 'additionalProperties': False},
        lookup,
        'shelf-v1',
    )
    fx.queue_calls([{'id': 'native-1', 'name': 'shelf_lookup', 'arguments': json.dumps({'recordId': 'REC-1'})}])
    fx.queue_text('Shelf QA-104.')
    turn = runtime_for(fx, guards=[guard.guard]).turn(
        tagged(rule(stage), tools=fx.tools), message='Find REC-1', policy=fx.policy
    )
    return turn, calls


@pytest.mark.parametrize('provider', PROVIDERS)
@pytest.mark.parametrize('value', UNDELIVERABLE.values(), ids=UNDELIVERABLE.keys())
def test_before_tool_feedback_that_cannot_be_delivered_is_a_fault(provider, value):
    guard = recorder('shelf_check', Verdict(False, refused_values=(value,)), stages=TOOL_STAGES)
    with ConversationFixture(provider) as fx:
        turn, calls = tool_turn(fx, guard, 'before_tool')
        assert len(fx.raw_requests) == 1  # No dispatch after the fault.
    assert calls == [] and failure(turn) == 'ruleViolation'
    assert turn.rules_faulted == (RULE_ID,)


@pytest.mark.parametrize('provider', PROVIDERS)
@pytest.mark.parametrize('value', UNDELIVERABLE.values(), ids=UNDELIVERABLE.keys())
def test_after_tool_feedback_that_cannot_be_delivered_is_a_fault_and_keeps_effect_evidence(provider, value):
    guard = recorder('shelf_check', Verdict(False, refused_values=(value,)), stages=TOOL_STAGES)
    with ConversationFixture(provider) as fx:
        turn, calls = tool_turn(fx, guard, 'after_tool')
        assert len(fx.raw_requests) == 1
    assert calls == [{'recordId': 'REC-1'}] and failure(turn) == 'ruleViolation'
    assert turn.rules_faulted == (RULE_ID,)
    outcome = turn.evidence.to_dict()['toolOutcomes'][0]['outcome']
    assert outcome == {'status': 'ok', 'result': {'shelfMark': 'QA-104'}}


@pytest.mark.parametrize('provider', PROVIDERS)
@pytest.mark.parametrize('echo', [True, False])
@pytest.mark.parametrize('value', UNDELIVERABLE.values(), ids=UNDELIVERABLE.keys())
def test_answer_feedback_that_cannot_be_delivered_is_a_fault(provider, echo, value):
    guard = recorder('shelf_check', Verdict(False, refused_values=(value,)), echo_values=echo)
    with ConversationFixture(provider) as fx:
        fx.queue_text('Shelf QA-104.')
        turn = runtime_for(fx, guards=[guard.guard]).turn(tagged(rule('final')), message='Go', policy=fx.policy)
        assert len(fx.raw_requests) == 1
    assert failure(turn) == 'ruleViolation' and turn.rules_faulted == (RULE_ID,)


@pytest.mark.parametrize('provider', PROVIDERS)
def test_ordinary_long_values_and_large_integers_are_still_delivered(provider):
    long_text, large = 'QA-' + 'x' * 5000, 10**2000
    guard = recorder('shelf_check', Verdict(False, refused_values=(long_text, large)), echo_values=True)
    with ConversationFixture(provider) as fx:
        fx.queue_text('Shelf QA-999.')
        fx.queue_text('Shelf QA-104.')
        turn = runtime_for(fx, guards=[guard.guard]).turn(tagged(rule('final')), message='Go', policy=fx.policy)
        feedback = json.dumps(json.loads(fx.raw_requests[1]), ensure_ascii=False)
    assert turn.result.stdout == 'Shelf QA-104.' and turn.rules_faulted == ()
    assert 'QA-' + 'x' * 2045 + '…' in feedback and 'x' * 2046 not in feedback  # Elided to 2,048 characters.
    assert str(large) in feedback


@pytest.mark.parametrize('provider', PROVIDERS)
@pytest.mark.parametrize('stage', TOOL_STAGES)
def test_many_long_tool_stage_values_still_retry_within_the_feedback_budget(provider, stage):
    values = tuple(f'QA-{index}-' + 'x' * 2100 for index in range(64))
    guard = recorder('shelf_check', Verdict(False, refused_values=values), stages=TOOL_STAGES)
    with ConversationFixture(provider) as fx:
        turn, calls = tool_turn(fx, guard, stage)
        feedback = fx.raw_requests[1]
    assert failure(turn) is None and turn.result.stdout == 'Shelf QA-104.' and turn.rules_faulted == ()
    assert b'refusedCount' in feedback and b'QA-0-' in feedback and b'QA-63-' not in feedback


@pytest.mark.parametrize('provider', PROVIDERS)
def test_many_wide_answer_values_and_a_long_rule_still_retry_within_the_feedback_budget(provider):
    text = 'Quote shelf marks exactly, ' + 'and keep them short ' * 300
    body = f'- {text}<!-- enforce: shelf_check(stage=final, on_violation=retry) -->\n'
    guard = recorder('shelf_check', Verdict(False, refused_values=('😀' * 2048,) * 64), echo_values=True)
    with ConversationFixture(provider) as fx:
        fx.queue_text('Shelf QA-999.')
        fx.queue_text('Shelf QA-104.')
        turn = runtime_for(fx, guards=[guard.guard]).turn(tagged(body), message='Go', policy=fx.policy)
        payload = json.loads(fx.raw_requests[1])
    assert failure(turn) is None and turn.result.stdout == 'Shelf QA-104.' and turn.rules_faulted == ()
    messages = payload['messages']
    content = messages[-1]['content']
    content = content if isinstance(content, str) else content[0]['text']
    feedback = json.loads(content.split('\n', 1)[1])['ruleFeedback']
    (entry,) = feedback['violations']
    assert entry['rule'].endswith('…') and len(entry['rule']) <= 513 and entry['refusedCount'] == 64
    assert len(json.dumps(feedback, ensure_ascii=False).encode()) <= 16_384


def test_integers_of_up_to_2048_digits_are_delivered_and_longer_ones_are_faults():
    from prosaic_runtime.rule_hooks import _refused

    assert _refused((10**2047,)) == [10**2047]  # 2,048 digits.
    assert _refused((-(10**2048 - 1),)) == [-(10**2048 - 1)]
    with pytest.raises(ValueError):
        _refused((10**2048,))  # 2,049 digits.


@pytest.mark.parametrize('provider', PROVIDERS)
def test_a_model_sent_unencodable_id_is_a_malformed_request_on_both_wires(provider):
    from prosaic_runtime import generic_guards

    calls = []
    body = (
        '- Look up only seen records. '
        '<!-- enforce: args_reference_seen_ids(keys=["recordId"], stage=before_tool, on_violation=retry) -->\n'
    )
    with ConversationFixture(provider) as fx:
        fx.tools['shelf_lookup'] = CustomTool(
            'shelf_lookup',
            'Look up a synthetic shelf mark',
            {'type': 'object', 'properties': {'recordId': {'type': 'string'}}, 'additionalProperties': False},
            lambda arguments: calls.append(arguments) or {'shelfMark': 'QA-104'},
            'shelf-v1',
        )
        fx.queue_calls(
            [{'id': 'native-1', 'name': 'shelf_lookup', 'arguments': json.dumps({'recordId': 'REC-\ud800'})}]
        )
        turn = runtime_for(fx, guards=generic_guards()).turn(
            tagged(body, tools=fx.tools), message='Find REC-1', policy=fx.policy
        )
    assert calls == [] and failure(turn) == 'invalidToolRequest' and turn.rules_faulted == ()


def _envelope(violations, *, user_role=False):
    from types import SimpleNamespace

    from prosaic_runtime.rule_hooks import RuleHooks

    entries = [
        (SimpleNamespace(text=text, guard=SimpleNamespace(echo_values=echo)), Verdict(False, refused_values=values))
        for text, values, echo in violations
    ]
    feedback = RuleHooks._feedback(None, entries, user_role=user_role)
    return feedback, len(json.dumps({'ruleFeedback': feedback}, ensure_ascii=False).encode())


@pytest.mark.parametrize(
    'violations,user_role',
    [
        ([('Quote shelf marks exactly.', ('x' * 283,) * 64, True)], False),
        ([(f'Rule {n} text.', ('y' * 49,) * 64, True) for n in range(5)], False),
        ([(f'Rule {n} ' + 'z' * 600, ('v',), False) for n in range(80)], True),
        ([(f'Rule {n} ' + 'z' * 600, ('v',), False) for n in range(256)], True),
        ([('😀' * 600, ('😀' * 2048,) * 64, True)], True),
    ],
)
def test_the_feedback_envelope_never_exceeds_its_budget(violations, user_role):
    feedback, size = _envelope(violations, user_role=user_role)
    assert size <= 16_384
    kept = len(feedback['violations']) + feedback.get('omittedViolations', 0)
    assert kept == len(violations) and feedback['violations']  # Every rule is quoted or counted.


def test_feedback_that_fits_is_delivered_whole():
    feedback, _ = _envelope([('Quote shelf marks exactly.', ('QA-1', 'QA-2'), True)])
    assert feedback == {
        'version': 1,
        'violations': [{'rule': 'Quote shelf marks exactly.', 'refusedValues': ['QA-1', 'QA-2']}],
    }


@pytest.mark.parametrize('provider', PROVIDERS)
@pytest.mark.parametrize('max_event_bytes', [65_536, 4096])
def test_a_long_accepted_answer_is_delivered_in_events_that_fit(provider, max_event_bytes):
    from prosaic_runtime import TurnLimits

    answer = '\n'.join(f'Line {n}: "QA-104" shelf' for n in range(2500 if max_event_bytes > 4096 else 200))
    guard = recorder('shelf_check')
    events = []
    with ConversationFixture(provider) as fx:
        fx.queue_stream_text(answer)
        turn = runtime_for(fx, guards=[guard.guard]).turn(
            tagged(rule('final')),
            message='Go',
            policy=fx.policy,
            limits=TurnLimits(max_event_bytes=max_event_bytes),
            on_event=events.append,
        )
    assert failure(turn) is None and turn.result.stdout == answer
    assert ''.join(e['text'] for e in events if e['event'] == 'text_delta') == answer
