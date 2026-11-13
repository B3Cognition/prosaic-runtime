"""A fix guard that declines to repair falls back to the rule's retry path; it is never a guard fault."""

import json

import pytest

from prosaic_runtime import CustomTool, RunPolicy, Verdict
from support.conversation_server import ConversationFixture
from support.rules_support import recorder, runtime_for, tagged

PROVIDERS = ['openai-compatible', 'anthropic']
TOOL_STAGES = ('before_tool', 'after_tool')
RULE_ID = 'subagents/shelf-keeper.md:1:shelf_check'
DECLINE = Verdict(False, reason='cannot repair this draft', refused_values=('QA-999',))


def rule(stage, action='fix', text='Quote shelf marks exactly.'):
    return f'- {text} <!-- enforce: shelf_check(stage={stage}, on_violation={action}) -->\n'


def failure(turn):
    return turn.evidence.to_dict()['failureCode']


def answer_turn(fx, guard, *answers, stage='final', policy=None):
    for answer in answers:
        fx.queue_text(answer)
    return runtime_for(fx, guards=[guard.guard]).turn(
        tagged(rule(stage)), message='Where is the tide atlas?', policy=policy or fx.policy
    )


def tool_turn(fx, guard, stage):
    calls = []

    def lookup(arguments):
        calls.append(arguments)
        return {'shelfMark': 'QA-104', 'title': 'Tide Tables of Port Wren'}

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
@pytest.mark.parametrize('stage', ['before_answer', 'final'])
def test_a_declined_answer_repair_retries_and_the_corrected_answer_is_accepted(provider, stage):
    guard = recorder('shelf_check', DECLINE, Verdict(True))
    with ConversationFixture(provider) as fx:
        turn = answer_turn(fx, guard, 'Shelf QA-999.', 'Shelf QA-104.', stage=stage)
        assert len(fx.raw_requests) == 2
        feedback = json.dumps(json.loads(fx.raw_requests[1])['messages'][-1])
    assert turn.result.exit_code == 0 and failure(turn) is None and turn.result.stdout == 'Shelf QA-104.'
    # The same envelope an ordinary retry sends: the rule text, values counted (no echo opt-in).
    assert 'Quote shelf marks exactly.' in feedback and 'refusedCount' in feedback
    assert 'cannot repair this draft' not in feedback and 'QA-999' not in feedback
    assert turn.rules_declined == turn.rules_retried == turn.rules_violated == (RULE_ID,)
    assert turn.rules_faulted == () and turn.rules_fixed == ()
    assert [m.role for m in turn.completed_turn.messages] == ['user', 'assistant']


@pytest.mark.parametrize('provider', PROVIDERS)
def test_a_decline_then_a_second_violation_is_a_rule_violation_not_a_fault(provider):
    guard = recorder('shelf_check', DECLINE, DECLINE)
    with ConversationFixture(provider) as fx:
        turn = answer_turn(fx, guard, 'Shelf QA-999.', 'Shelf QA-998.')
        assert len(fx.raw_requests) == 2  # Exactly one retry, the default allowance.
    assert turn.result.exit_code == 1 and turn.completed_turn is None and failure(turn) == 'ruleViolation'
    assert turn.rules_declined == turn.rules_violated == (RULE_ID,) and turn.rules_faulted == ()


@pytest.mark.parametrize('provider', PROVIDERS)
def test_a_decline_without_a_request_left_stops_as_a_violation_not_a_fault(provider):
    guard = recorder('shelf_check', DECLINE)
    with ConversationFixture(provider) as fx:
        policy = RunPolicy(max_provider_requests=1, max_tool_calls=0)
        turn = answer_turn(fx, guard, 'Shelf QA-999.', policy=policy)
        assert len(fx.raw_requests) == 1  # A decline never adds a request beyond the policy.
    assert failure(turn) == 'ruleViolation' and turn.rules_faulted == ()
    assert turn.rules_declined == (RULE_ID,) and turn.rules_retried == ()


@pytest.mark.parametrize('provider', PROVIDERS)
def test_a_declined_before_tool_repair_refuses_the_call_with_feedback(provider):
    guard = recorder('shelf_check', Verdict(False, refused_values=('REC-1',)), stages=TOOL_STAGES)
    with ConversationFixture(provider) as fx:
        turn, calls = tool_turn(fx, guard, 'before_tool')
        follow_up = fx.raw_requests[1].decode()
    assert calls == [] and turn.result.exit_code == 0 and turn.result.stdout == 'Shelf QA-104.'
    assert 'rule_refused' in follow_up and 'Quote shelf marks exactly.' in follow_up and 'REC-1' in follow_up
    assert turn.rules_declined == turn.rules_retried == (RULE_ID,) and turn.rules_faulted == ()


@pytest.mark.parametrize('provider', PROVIDERS)
def test_a_declined_after_tool_repair_replaces_the_result_without_rerunning_the_tool(provider):
    guard = recorder('shelf_check', Verdict(False), stages=TOOL_STAGES)
    with ConversationFixture(provider) as fx:
        turn, calls = tool_turn(fx, guard, 'after_tool')
        follow_up = fx.raw_requests[1].decode()
    assert calls == [{'recordId': 'REC-1'}] and turn.result.exit_code == 0
    assert 'rule_refused' in follow_up and 'Tide Tables' not in follow_up
    assert turn.evidence.to_dict()['toolOutcomes'][0]['outcome']['result']['title'] == 'Tide Tables of Port Wren'
    assert turn.rules_declined == turn.rules_retried == (RULE_ID,) and turn.rules_faulted == ()


@pytest.mark.parametrize('provider', PROVIDERS)
@pytest.mark.parametrize(
    'verdict',
    [RuntimeError('guard bug'), Verdict(False, fixed=7), Verdict(False, fixed={'not': 'text'}), 'not a verdict'],
    ids=['raises', 'wrong-type-int', 'wrong-type-object', 'not-a-verdict'],
)
def test_a_raising_fixer_or_an_unusable_repair_is_still_a_fault(provider, verdict):
    guard = recorder('shelf_check', verdict)
    with ConversationFixture(provider) as fx:
        turn = answer_turn(fx, guard, 'Shelf QA-999.')
        assert len(fx.raw_requests) == 1
    assert failure(turn) == 'ruleViolation' and turn.rules_faulted == (RULE_ID,)
    assert turn.rules_declined == () and turn.rules_retried == ()


@pytest.mark.parametrize('provider', PROVIDERS)
def test_a_succeeding_fixer_is_unchanged(provider):
    guard = recorder('shelf_check', Verdict(False, fixed='Shelf QA-104.'), Verdict(True))
    with ConversationFixture(provider) as fx:
        turn = answer_turn(fx, guard, 'Shelf **QA-104**.')
        assert len(fx.raw_requests) == 1
    assert turn.result.stdout == 'Shelf QA-104.' and turn.rules_fixed == (RULE_ID,)
    assert turn.rules_declined == turn.rules_retried == turn.rules_faulted == ()


@pytest.mark.parametrize('provider', PROVIDERS)
def test_a_decline_is_judged_after_another_rules_repair(provider):
    body = '- Unwrap marks. <!-- enforce: unwrap_check(stage=before_answer, on_violation=fix) -->\n' + rule('final')
    fixer = recorder('unwrap_check', Verdict(False, fixed='Shelf QA-999.'))
    decliner = recorder('shelf_check', DECLINE, DECLINE, Verdict(True))
    with ConversationFixture(provider) as fx:
        fx.queue_text('Shelf **QA-999**.')
        fx.queue_text('Shelf QA-104.')
        turn = runtime_for(fx, guards=[fixer.guard, decliner.guard]).turn(tagged(body), message='Go', policy=fx.policy)
    # The decline in the repairing pass is stale; the recheck on the repaired draft decides the retry.
    assert decliner.calls[1][1].draft == 'Shelf QA-999.'
    assert turn.result.stdout == 'Shelf QA-104.' and turn.rules_declined == (RULE_ID.replace('1:', '2:'),)


@pytest.mark.parametrize('provider', PROVIDERS)
def test_an_ordinary_retry_is_reported_as_retried_not_declined(provider):
    guard = recorder('shelf_check', Verdict(False), Verdict(True))
    with ConversationFixture(provider) as fx:
        fx.queue_text('Shelf QA-999.')
        fx.queue_text('Shelf QA-104.')
        turn = runtime_for(fx, guards=[guard.guard]).turn(
            tagged(rule('final', 'retry')), message='Where?', policy=fx.policy
        )
    assert turn.result.exit_code == 0 and turn.rules_retried == (RULE_ID,) and turn.rules_declined == ()
