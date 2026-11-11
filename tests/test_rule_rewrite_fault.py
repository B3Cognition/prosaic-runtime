"""A before_tool repair the tool schema refuses is a guard fault, reported in rules_faulted as documented."""

import json

import pytest

from prosaic_runtime import CustomTool, Verdict
from support.conversation_server import ConversationFixture
from support.rules_support import recorder, runtime_for, tagged

PROVIDERS = ['openai-compatible', 'anthropic']
TOOL_STAGES = ('before_tool', 'after_tool')
SCHEMA = {'type': 'object', 'properties': {'recordId': {'type': 'string'}}, 'additionalProperties': False}


def fix_rule(name, line_text):
    return f'- {line_text} <!-- enforce: {name}(stage=before_tool, on_violation=fix) -->\n'


def lookup_turn(fx, guards, body, calls):
    effects = []
    fx.tools['shelf_lookup'] = CustomTool(
        'shelf_lookup',
        'Look up a synthetic shelf mark',
        SCHEMA,
        lambda arguments: effects.append(arguments) or {'shelfMark': 'QA-104'},
        'shelf-v1',
    )
    fx.queue_calls(calls)
    fx.queue_text('Shelf QA-104.')
    turn = runtime_for(fx, guards=guards).turn(tagged(body, tools=fx.tools), message='Find REC-1', policy=fx.policy)
    return turn, effects


@pytest.mark.parametrize('provider', PROVIDERS)
def test_a_rewrite_outside_the_tool_schema_is_a_reported_fault(provider):
    guard = recorder('shelf_check', Verdict(False, fixed={'recordId': 7}), stages=TOOL_STAGES)
    rule_id = 'subagents/shelf-keeper.md:1:shelf_check'
    with ConversationFixture(provider) as fx:
        turn, effects = lookup_turn(
            fx,
            [guard.guard],
            fix_rule('shelf_check', 'Use canonical ids.'),
            [{'id': 'native-1', 'name': 'shelf_lookup', 'arguments': '{"recordId": "REC-1"}'}],
        )
        assert len(fx.raw_requests) == 1
    assert effects == [] and turn.evidence.to_dict()['failureCode'] == 'ruleViolation'
    assert turn.rules_faulted == turn.rules_violated == (rule_id,)
    assert turn.rules_fixed == ()  # A repair that could not be used repaired nothing.


@pytest.mark.parametrize('provider', PROVIDERS)
def test_a_refused_batch_faults_only_the_culprit_and_fixes_nothing(provider):
    first = recorder('shelf_check', Verdict(False, fixed={'recordId': 'REC-2'}), stages=TOOL_STAGES)
    # Second rule: accepts call 1 (both passes), then rewrites call 2 outside the schema.
    second = recorder(
        'id_check', Verdict(True), Verdict(True), Verdict(False, fixed={'recordId': 7}), stages=TOOL_STAGES
    )
    body = fix_rule('shelf_check', 'Use canonical ids.') + fix_rule('id_check', 'Use string ids.')
    calls = [
        {'id': 'native-1', 'name': 'shelf_lookup', 'arguments': json.dumps({'recordId': 'REC-1'})},
        {'id': 'native-2', 'name': 'shelf_lookup', 'arguments': json.dumps({'recordId': 'REC-3'})},
    ]
    with ConversationFixture(provider) as fx:
        turn, effects = lookup_turn(fx, [first.guard, second.guard], body, calls)
    assert effects == [] and turn.evidence.to_dict()['failureCode'] == 'ruleViolation'
    assert turn.rules_faulted == ('subagents/shelf-keeper.md:2:id_check',)
    # The batch never ran, so the innocent rule's valid repair repaired nothing either; only the culprit faults.
    assert turn.rules_fixed == ()
    assert turn.rules_violated == ('subagents/shelf-keeper.md:1:shelf_check', 'subagents/shelf-keeper.md:2:id_check')


@pytest.mark.parametrize('provider', PROVIDERS)
def test_a_repair_from_an_earlier_batch_that_ran_stays_fixed(provider):
    # Round 1: the repair runs. Round 2: the same rule's repair is refused by the schema.
    guard = recorder(
        'shelf_check',
        Verdict(False, fixed={'recordId': 'REC-2'}),
        Verdict(True),
        Verdict(False, fixed={'recordId': 7}),
        stages=TOOL_STAGES,
    )
    rule_id = 'subagents/shelf-keeper.md:1:shelf_check'
    with ConversationFixture(provider) as fx:
        fx.tools['shelf_lookup'] = CustomTool(
            'shelf_lookup',
            'Look up a synthetic shelf mark',
            SCHEMA,
            lambda arguments: {'shelfMark': 'QA-104'},
            'shelf-v1',
        )
        fx.queue_calls([{'id': 'native-1', 'name': 'shelf_lookup', 'arguments': '{"recordId": "REC-1"}'}])
        fx.queue_calls([{'id': 'native-2', 'name': 'shelf_lookup', 'arguments': '{"recordId": "REC-3"}'}])
        turn = runtime_for(fx, guards=[guard.guard]).turn(
            tagged(fix_rule('shelf_check', 'Use canonical ids.'), tools=fx.tools),
            message='Find REC-1',
            policy=fx.policy,
        )
    assert turn.evidence.to_dict()['failureCode'] == 'ruleViolation'
    assert turn.rules_faulted == turn.rules_fixed == (rule_id,)  # Fixed in round 1, faulted in round 2.


@pytest.mark.parametrize('provider', PROVIDERS)
def test_an_unnamed_refusal_still_faults_only_the_culprit(provider, monkeypatch):
    from prosaic_runtime.tool_registry import BoundedToolRegistry, InvalidToolRequest

    admit = BoundedToolRegistry.validate_batch

    def unnamed(self, calls):  # Admission that refuses a batch without naming the call.
        try:
            return admit(self, calls)
        except InvalidToolRequest:
            raise InvalidToolRequest() from None

    monkeypatch.setattr(BoundedToolRegistry, 'validate_batch', unnamed)
    first = recorder('shelf_check', Verdict(False, fixed={'recordId': 'REC-2'}), stages=TOOL_STAGES)
    second = recorder(
        'id_check', Verdict(True), Verdict(True), Verdict(False, fixed={'recordId': 7}), stages=TOOL_STAGES
    )
    body = fix_rule('shelf_check', 'Use canonical ids.') + fix_rule('id_check', 'Use string ids.')
    calls = [
        {'id': 'native-1', 'name': 'shelf_lookup', 'arguments': json.dumps({'recordId': 'REC-1'})},
        {'id': 'native-2', 'name': 'shelf_lookup', 'arguments': json.dumps({'recordId': 'REC-3'})},
    ]
    with ConversationFixture(provider) as fx:
        turn, effects = lookup_turn(fx, [first.guard, second.guard], body, calls)
    assert effects == [] and turn.rules_faulted == ('subagents/shelf-keeper.md:2:id_check',)
    assert turn.rules_fixed == ()
