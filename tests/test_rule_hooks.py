"""Rule hooks inside the existing turn loop, on both real provider wires."""

import json

import pytest

from prosaic_runtime import CustomTool, RunPolicy, Verdict
from support.conversation_server import ConversationFixture
from support.rules_support import recorder, runtime_for, tagged

PROVIDERS = ['openai-compatible', 'anthropic']
TOOL_STAGES = ('before_tool', 'after_tool')
RULE_ID = 'subagents/shelf-keeper.md:1:shelf_check'


def rule(stage, action, text='Quote shelf marks exactly.'):
    return f'- {text} <!-- enforce: shelf_check(stage={stage}, on_violation={action}) -->\n'


def failure(turn):
    return turn.evidence.to_dict()['failureCode']


def wire(fx, index):
    return fx.raw_requests[index].decode()


def user_feedback(fx, index):
    """The unprivileged messages of a request, excluding the system/instruction role."""
    payload = json.loads(fx.raw_requests[index])
    messages = payload['messages']
    if fx.provider != 'anthropic':
        messages = [m for m in messages if m['role'] != 'system']
    return json.dumps(messages), payload.get('system') or json.loads(fx.raw_requests[index])['messages'][0]['content']


def add_lookup(fx, calls):
    def lookup(arguments):
        calls.append(arguments)
        return {'shelfMark': 'QA-104', 'title': 'Tide Tables of Port Wren'}

    fx.tools['shelf_lookup'] = CustomTool(
        'shelf_lookup',
        'Look up a synthetic shelf mark',
        {
            'type': 'object',
            'properties': {'recordId': {'type': 'string'}},
            'required': ['recordId'],
            'additionalProperties': False,
        },
        lookup,
        'shelf-v1',
    )


def tool_turn(fx, guard, stage, action, *, answer='Shelf QA-104.', policy=None):
    calls = []
    add_lookup(fx, calls)
    fx.queue_calls([{'id': 'native-1', 'name': 'shelf_lookup', 'arguments': json.dumps({'recordId': 'REC-1'})}])
    if answer is not None:
        fx.queue_text(answer)
    turn = runtime_for(fx, guards=[guard.guard]).turn(
        tagged(rule(stage, action), tools=fx.tools), message='Find REC-1', policy=policy or fx.policy
    )
    return turn, calls


@pytest.mark.parametrize('provider', PROVIDERS)
@pytest.mark.parametrize('echo', [False, True])
def test_before_answer_retry_quotes_rule_and_refused_values_as_unprivileged_data(provider, echo):
    guard = recorder(
        'shelf_check', Verdict(False, reason='guard-private-reason', refused_values=('QA-999',)), echo_values=echo
    )
    with ConversationFixture(provider) as fx:
        fx.queue_text('Shelf QA-999.')
        fx.queue_text('Shelf QA-104.')
        turn = runtime_for(fx, guards=[guard.guard]).turn(
            tagged(rule('before_answer', 'retry')), message='Where?', policy=fx.policy
        )
        unprivileged, system = user_feedback(fx, 1)
        assert system == user_feedback(fx, 0)[1]  # The instruction role never changes.
        feedback = json.loads(fx.raw_requests[1])['messages'][-1]
    assert feedback['role'] == 'user'
    feedback = json.dumps(feedback)
    assert turn.result.exit_code == 0 and turn.result.stdout == 'Shelf QA-104.'
    assert 'Quote shelf marks exactly.' in feedback
    # Raw values reach the user role only for guards that opt in; others send a count.
    assert ('QA-999' in feedback) is echo and ('refusedCount' in feedback) is not echo
    assert 'guard-private-reason' not in unprivileged and 'enforce' not in unprivileged
    assert [m.role for m in turn.completed_turn.messages] == ['user', 'assistant']
    assert 'QA-999' not in json.dumps(turn.completed_turn.to_dict())
    assert turn.evidence.to_dict()['providerAttempts'] == 2
    assert turn.rules_violated == (RULE_ID,) and turn.rules_evaluated == (RULE_ID,)
    assert guard.calls[0][1].draft == 'Shelf QA-999.' and guard.calls[0][1].user_message == 'Where?'


@pytest.mark.parametrize('provider', PROVIDERS)
def test_one_retry_per_turn_then_safe_failure(provider):
    bad = Verdict(False, refused_values=('QA-999',))
    with ConversationFixture(provider) as fx:
        fx.queue_text('Shelf QA-999.')
        fx.queue_text('Shelf QA-999 again.')
        turn = runtime_for(fx, guards=[recorder('shelf_check', bad, bad).guard]).turn(
            tagged(rule('final', 'retry')), message='Where?', policy=fx.policy
        )
        assert len(fx.raw_requests) == 2
    assert turn.result.exit_code == 1 and turn.completed_turn is None and turn.result.stdout == ''
    assert failure(turn) == 'ruleViolation'


@pytest.mark.parametrize('provider', PROVIDERS)
def test_retry_never_adds_requests_beyond_the_policy(provider):
    with ConversationFixture(provider) as fx:
        fx.queue_text('Shelf QA-999.')
        policy = RunPolicy(max_provider_requests=1, max_tool_calls=0)
        turn = runtime_for(fx, guards=[recorder('shelf_check', Verdict(False)).guard]).turn(
            tagged(rule('final', 'retry')), message='Where?', policy=policy
        )
        assert len(fx.raw_requests) == 1
    assert failure(turn) == 'ruleViolation'


@pytest.mark.parametrize('provider', PROVIDERS)
@pytest.mark.parametrize(
    'action,verdicts',
    [
        ('fail', [Verdict(False)]),
        ('retry', [RuntimeError('guard bug')]),
        ('fix', [Verdict(False, fixed=None)]),
        ('retry', ['not a verdict']),
        ('fix', [Verdict(False, fixed=7)]),
    ],
)
def test_fail_raise_and_invalid_verdicts_are_safe_failures_never_exceptions(provider, action, verdicts):
    with ConversationFixture(provider) as fx:
        fx.queue_text('Shelf QA-999.')
        turn = runtime_for(fx, guards=[recorder('shelf_check', *verdicts).guard]).turn(
            tagged(rule('final', action)), message='Where?', policy=fx.policy
        )
        assert len(fx.raw_requests) == 1
    assert turn.result.exit_code == 1 and failure(turn) == 'ruleViolation'


@pytest.mark.parametrize('provider', PROVIDERS)
def test_fix_repairs_the_answer_and_is_rechecked(provider):
    guard = recorder('shelf_check', Verdict(False, fixed='Shelf QA-104.'), Verdict(True))
    with ConversationFixture(provider) as fx:
        fx.queue_text('Shelf **QA-104**.')
        turn = runtime_for(fx, guards=[guard.guard]).turn(
            tagged(rule('final', 'fix')), message='Where?', policy=fx.policy
        )
    assert turn.result.stdout == 'Shelf QA-104.'
    assert turn.completed_turn.to_dict()['messages'][-1]['content'] == [{'type': 'text', 'text': 'Shelf QA-104.'}]
    assert turn.rules_fixed == (RULE_ID,) and len(guard.calls) == 2  # Repair, then recheck.


@pytest.mark.parametrize('provider', PROVIDERS)
def test_fix_is_bounded_to_three_passes(provider):
    endless = [Verdict(False, fixed=f'pass {n}') for n in range(10)]
    guard = recorder('shelf_check', *endless)
    with ConversationFixture(provider) as fx:
        fx.queue_text('draft')
        turn = runtime_for(fx, guards=[guard.guard]).turn(tagged(rule('final', 'fix')), message='Go', policy=fx.policy)
    assert failure(turn) == 'ruleViolation' and len(guard.calls) <= 4


@pytest.mark.parametrize('provider', PROVIDERS)
def test_a_fix_is_never_success_on_its_own(provider):
    body = (
        rule('before_answer', 'fix')
        + '- Never print shelf zero. <!-- enforce: zero_check(stage=final, on_violation=fail) -->\n'
    )
    fixer = recorder('shelf_check', Verdict(False, fixed='Shelf QA-000.'))
    checker = recorder('zero_check', Verdict(False), Verdict(False))  # Consistently fails the repaired text.
    with ConversationFixture(provider) as fx:
        fx.queue_text('Shelf **QA-104**.')
        turn = runtime_for(fx, guards=[fixer.guard, checker.guard]).turn(tagged(body), message='Go', policy=fx.policy)
    assert failure(turn) == 'ruleViolation'
    assert checker.calls[0][1].draft == 'Shelf QA-000.'


@pytest.mark.parametrize('provider', PROVIDERS)
def test_before_tool_rewrite_is_what_runs_and_what_wire_and_history_record(provider):
    guard = recorder('shelf_check', Verdict(False, fixed={'recordId': 'REC-2'}), stages=TOOL_STAGES)
    with ConversationFixture(provider) as fx:
        turn, calls = tool_turn(fx, guard, 'before_tool', 'fix')
        follow_up = wire(fx, 1)
    assert calls == [{'recordId': 'REC-2'}]
    assert 'REC-2' in follow_up and follow_up.count('REC-1') == 1  # Only the user's own message.
    tool_call = turn.completed_turn.to_dict()['messages'][1]['content'][-1]
    assert tool_call['type'] == 'toolCall' and tool_call['arguments'] == {'recordId': 'REC-2'}
    assert turn.rules_fixed == (RULE_ID,)
    assert guard.calls[0][1].arguments == {'recordId': 'REC-1'} and guard.calls[0][1].tool_name == 'shelf_lookup'


@pytest.mark.parametrize('provider', PROVIDERS)
def test_before_tool_retry_returns_feedback_without_running_the_tool(provider):
    guard = recorder('shelf_check', Verdict(False, refused_values=('REC-1',)), stages=TOOL_STAGES)
    with ConversationFixture(provider) as fx:
        turn, calls = tool_turn(fx, guard, 'before_tool', 'retry')
        follow_up = wire(fx, 1)
    assert calls == [] and turn.result.exit_code == 0
    assert 'Quote shelf marks exactly.' in follow_up and 'REC-1' in follow_up and 'rule_refused' in follow_up


@pytest.mark.parametrize('provider', PROVIDERS)
def test_before_tool_fail_stops_before_any_effect(provider):
    guard = recorder('shelf_check', Verdict(False), stages=TOOL_STAGES)
    with ConversationFixture(provider) as fx:
        turn, calls = tool_turn(fx, guard, 'before_tool', 'fail', answer=None)
        assert len(fx.raw_requests) == 1
    assert calls == [] and failure(turn) == 'ruleViolation' and turn.evidence.to_dict()['toolOutcomes'] == []


@pytest.mark.parametrize('provider', PROVIDERS)
def test_before_tool_rewrite_must_still_match_the_tool_schema(provider):
    guard = recorder('shelf_check', Verdict(False, fixed={'recordId': 7}), stages=TOOL_STAGES)
    with ConversationFixture(provider) as fx:
        turn, calls = tool_turn(fx, guard, 'before_tool', 'fix', answer=None)
    assert calls == [] and failure(turn) == 'ruleViolation'


@pytest.mark.parametrize('provider', PROVIDERS)
def test_after_tool_fix_repairs_what_the_model_sees_and_keeps_effect_evidence(provider):
    repaired = {'status': 'ok', 'result': {'shelfMark': 'QA-104'}}
    guard = recorder('shelf_check', Verdict(False, fixed=repaired), Verdict(True), stages=TOOL_STAGES)
    with ConversationFixture(provider) as fx:
        turn, calls = tool_turn(fx, guard, 'after_tool', 'fix')
        follow_up = wire(fx, 1)
    assert calls == [{'recordId': 'REC-1'}] and 'Tide Tables' not in follow_up
    evidence = turn.evidence.to_dict()['toolOutcomes'][0]['outcome']
    assert evidence['result']['title'] == 'Tide Tables of Port Wren'
    assert guard.calls[0][1].tool_result == {
        'status': 'ok',
        'result': {'shelfMark': 'QA-104', 'title': 'Tide Tables of Port Wren'},
    }


@pytest.mark.parametrize('provider', PROVIDERS)
def test_after_tool_retry_replaces_the_result_without_rerunning_the_tool(provider):
    guard = recorder('shelf_check', Verdict(False, refused_values=('QA-104',)), stages=TOOL_STAGES)
    with ConversationFixture(provider) as fx:
        turn, calls = tool_turn(fx, guard, 'after_tool', 'retry')
        follow_up = wire(fx, 1)
    assert calls == [{'recordId': 'REC-1'}] and turn.result.exit_code == 0
    assert 'rule_refused' in follow_up and 'Tide Tables' not in follow_up
    assert turn.evidence.to_dict()['toolOutcomes'][0]['outcome']['status'] == 'ok'


@pytest.mark.parametrize('provider', PROVIDERS)
def test_after_tool_fail_keeps_effect_evidence(provider):
    guard = recorder('shelf_check', Verdict(False), stages=TOOL_STAGES)
    with ConversationFixture(provider) as fx:
        turn, calls = tool_turn(fx, guard, 'after_tool', 'fail', answer=None)
    assert calls == [{'recordId': 'REC-1'}] and failure(turn) == 'ruleViolation'
    assert turn.evidence.to_dict()['toolOutcomes'][0]['outcome']['status'] == 'ok'


@pytest.mark.parametrize('provider', PROVIDERS)
def test_the_single_retry_is_shared_across_stages(provider):
    body = rule('before_tool', 'retry') + '- Answer briefly. <!-- enforce: brief_check(on_violation=retry) -->\n'
    tool_guard = recorder('shelf_check', Verdict(False), stages=TOOL_STAGES)
    answer_guard = recorder('brief_check', Verdict(False))
    with ConversationFixture(provider) as fx:
        add_lookup(fx, [])
        fx.queue_calls([{'id': 'native-1', 'name': 'shelf_lookup', 'arguments': json.dumps({'recordId': 'REC-1'})}])
        fx.queue_text('A long answer.')
        turn = runtime_for(fx, guards=[tool_guard.guard, answer_guard.guard]).turn(
            tagged(body, tools=fx.tools), message='Find REC-1', policy=fx.policy
        )
    assert failure(turn) == 'ruleViolation'


@pytest.mark.parametrize('provider', PROVIDERS)
def test_guard_context_carries_observed_facts(provider):
    tool_guard = recorder('shelf_check', stages=TOOL_STAGES)
    with ConversationFixture(provider) as fx:
        add_lookup(fx, [])
        fx.queue_calls([{'id': 'native-1', 'name': 'shelf_lookup', 'arguments': json.dumps({'recordId': 'REC-1'})}])
        fx.queue_text('Shelf QA-104.')
        body = rule('after_tool', 'retry') + '- Answer briefly. <!-- enforce: brief_check(on_violation=retry) -->\n'
        answer_guard = recorder('brief_check')
        runtime_for(fx, guards=[tool_guard.guard, answer_guard.guard]).turn(
            tagged(body, tools=fx.tools), message='Find REC-1', input_data={'branch': 'Gull Reach'}, policy=fx.policy
        )
    context = answer_guard.calls[0][1]
    assert context.stage == 'before_answer' or context.stage == 'final'
    assert context.tool_results == ({'shelfMark': 'QA-104', 'title': 'Tide Tables of Port Wren'},)
    assert context.input_data == {'branch': 'Gull Reach'} and context.rule_text == 'Answer briefly.'
    assert context.settings == {} and context.rule_id.endswith(':brief_check')


def test_streaming_retry_on_the_openai_wire():
    with ConversationFixture('openai-compatible') as fx:
        fx.queue_stream_text('Shelf QA-999.')
        fx.queue_stream_text('Shelf QA-104.')
        turn = runtime_for(fx, guards=[recorder('shelf_check', Verdict(False, refused_values=('QA-999',))).guard]).turn(
            tagged(rule('final', 'retry')), message='Where?', policy=fx.policy
        )
    assert turn.result.stdout == 'Shelf QA-104.' and turn.evidence.to_dict()['providerAttempts'] == 2


@pytest.mark.parametrize('provider', PROVIDERS)
def test_a_refusal_never_carries_over_to_a_later_round_reusing_the_call_id(provider):
    """Providers may reuse call ids across rounds; refusals are per batch."""
    guard = recorder('shelf_check', Verdict(False, refused_values=('REC-9',)), stages=TOOL_STAGES)
    calls = []
    with ConversationFixture(provider) as fx:
        add_lookup(fx, calls)
        for record in ('REC-9', 'REC-1'):
            fx.queue_calls([{'id': 'reused-id', 'name': 'shelf_lookup', 'arguments': json.dumps({'recordId': record})}])
        fx.queue_text('Shelf QA-104.')
        turn = runtime_for(fx, guards=[guard.guard]).turn(
            tagged(rule('before_tool', 'retry'), tools=fx.tools), message='Find REC-1', policy=fx.policy
        )
    assert turn.result.exit_code == 0 and calls == [{'recordId': 'REC-1'}]


@pytest.mark.parametrize('provider', PROVIDERS)
def test_only_results_the_model_saw_count_as_observed_facts(provider):
    """A refused or redacted tool result never counts as seen by later guards."""
    redacted = {'status': 'ok', 'result': {'shelfMark': 'QA-104'}}
    tool_guard = recorder('shelf_check', Verdict(False, fixed=redacted), Verdict(True), stages=TOOL_STAGES)
    answer_guard = recorder('brief_check')
    body = rule('after_tool', 'fix') + '- Answer briefly. <!-- enforce: brief_check(on_violation=retry) -->\n'
    with ConversationFixture(provider) as fx:
        add_lookup(fx, [])
        fx.queue_calls([{'id': 'native-1', 'name': 'shelf_lookup', 'arguments': json.dumps({'recordId': 'REC-1'})}])
        fx.queue_text('Shelf QA-104.')
        runtime_for(fx, guards=[tool_guard.guard, answer_guard.guard]).turn(
            tagged(body, tools=fx.tools), message='Find REC-1', policy=fx.policy
        )
    assert answer_guard.calls[0][1].tool_results == ({'shelfMark': 'QA-104'},)  # Redacted title is not a fact.
    assert tool_guard.calls[0][1].tool_results == ()  # The result under check is tool_result, not yet seen.


@pytest.mark.parametrize('provider', PROVIDERS)
def test_a_retry_replaced_result_is_not_an_observed_fact(provider):
    tool_guard = recorder('shelf_check', Verdict(False, refused_values=('QA-104',)), stages=TOOL_STAGES)
    answer_guard = recorder('brief_check')
    body = rule('after_tool', 'retry') + '- Answer briefly. <!-- enforce: brief_check(on_violation=fail) -->\n'
    with ConversationFixture(provider) as fx:
        add_lookup(fx, [])
        fx.queue_calls([{'id': 'native-1', 'name': 'shelf_lookup', 'arguments': json.dumps({'recordId': 'REC-1'})}])
        fx.queue_text('No shelf mark available.')
        runtime_for(fx, guards=[tool_guard.guard, answer_guard.guard]).turn(
            tagged(body, tools=fx.tools), message='Find REC-1', policy=fx.policy
        )
    assert answer_guard.calls[0][1].tool_results == ()


def test_each_check_gets_private_lazily_decoded_tool_results():
    """No per-check re-serialization; a guard mutating results cannot affect another."""
    from prosaic_runtime.rules import ToolResults

    encoded = [json.dumps({'figure': 1}), json.dumps({'figure': 2})]
    first, second = ToolResults(encoded), ToolResults(encoded)
    assert first._decoded == {} and len(first) == 2  # Nothing decoded until read.
    first[0]['figure'] = 99
    assert first[0] == {'figure': 99} and second[0] == {'figure': 1}
    assert first[-1] == {'figure': 2} and second[:1] == ({'figure': 1},) and list(reversed(second))[0] == {'figure': 2}
    with pytest.raises(IndexError):
        first[2]
    assert second == ({'figure': 1}, {'figure': 2}) and second != ({'figure': 1},)
