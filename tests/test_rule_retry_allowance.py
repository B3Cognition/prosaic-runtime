"""The host's per-turn rule retry allowance: ProsaicRuntime(rule_retries=...), default 1, bounded 0..3."""

from dataclasses import fields
import json

import pytest

import prosaic_runtime as api
from prosaic_runtime import CustomTool, ProsaicRuntime, RunPolicy, RuntimeConfig, TurnAdmissionError, Verdict
from support.conversation_server import ConversationFixture
from support.rules_support import recorder, tagged

PROVIDERS = ['openai-compatible', 'anthropic']
RULE_ID = 'subagents/shelf-keeper.md:1:shelf_check'
TOOL_STAGES = ('before_tool', 'after_tool')


def rule(stage='final', action='retry'):
    return f'- Quote shelf marks exactly. <!-- enforce: shelf_check(stage={stage}, on_violation={action}) -->\n'


def failure(turn):
    return turn.evidence.to_dict()['failureCode']


def runtime(fx, guards, retries=None):
    extra = {} if retries is None else {'rule_retries': retries}
    return ProsaicRuntime(fx.runtime.config, custom_tools=fx.tools, guards=list(guards), **extra)


def answers(fx, guard, texts, *, retries=None, body=None, policy=None):
    for text in texts:
        fx.queue_text(text)
    return runtime(fx, [guard.guard], retries).turn(
        tagged(body or rule()), message='Where is the tide atlas?', policy=policy or fx.policy
    )


@pytest.mark.parametrize('provider', PROVIDERS)
def test_the_default_allowance_is_one_retry_as_before(provider):
    guard = recorder('shelf_check', Verdict(False), Verdict(False), Verdict(True))
    with ConversationFixture(provider) as fx:
        turn = answers(fx, guard, ['Shelf QA-997.', 'Shelf QA-998.', 'Shelf QA-104.'])
        assert len(fx.raw_requests) == 2
    assert failure(turn) == 'ruleViolation' and turn.rule_retries_used == 1 and turn.rules_retried == (RULE_ID,)


@pytest.mark.parametrize('provider', PROVIDERS)
@pytest.mark.parametrize('allowance', [2, 3])
def test_a_larger_allowance_recovers_a_repeatedly_violating_turn(provider, allowance):
    guard = recorder('shelf_check', *[Verdict(False)] * allowance, Verdict(True))
    texts = [f'Shelf QA-9{n:02d}.' for n in range(allowance)] + ['Shelf QA-104.']
    with ConversationFixture(provider) as fx:
        turn = answers(fx, guard, texts, retries=allowance)
        assert len(fx.raw_requests) == allowance + 1
    assert turn.result.exit_code == 0 and turn.result.stdout == 'Shelf QA-104.'
    assert turn.rule_retries_used == allowance and turn.rules_retried == (RULE_ID,)


@pytest.mark.parametrize('provider', PROVIDERS)
@pytest.mark.parametrize('allowance', [2, 3])
def test_the_allowance_is_a_hard_bound(provider, allowance):
    guard = recorder('shelf_check', *[Verdict(False)] * (allowance + 1), Verdict(True))
    texts = [f'Shelf QA-9{n:02d}.' for n in range(allowance + 2)]
    with ConversationFixture(provider) as fx:
        turn = answers(fx, guard, texts, retries=allowance)
        assert len(fx.raw_requests) == allowance + 1
    assert failure(turn) == 'ruleViolation' and turn.rule_retries_used == allowance and turn.rules_faulted == ()


@pytest.mark.parametrize('provider', PROVIDERS)
@pytest.mark.parametrize('action', ['retry', 'fix'])
def test_zero_never_retries_a_violation_or_a_decline(provider, action):
    guard = recorder('shelf_check', Verdict(False), Verdict(True))
    with ConversationFixture(provider) as fx:
        turn = answers(fx, guard, ['Shelf QA-999.', 'Shelf QA-104.'], retries=0, body=rule(action=action))
        assert len(fx.raw_requests) == 1
    assert failure(turn) == 'ruleViolation' and turn.rules_faulted == ()
    assert turn.rule_retries_used == 0 and turn.rules_retried == ()


@pytest.mark.parametrize('provider', PROVIDERS)
def test_the_allowance_never_adds_requests_beyond_the_policy(provider):
    guard = recorder('shelf_check', Verdict(False), Verdict(False), Verdict(True))
    with ConversationFixture(provider) as fx:
        policy = RunPolicy(max_provider_requests=2, max_tool_calls=0)
        turn = answers(fx, guard, ['Shelf QA-998.', 'Shelf QA-999.', 'Shelf QA-104.'], retries=3, policy=policy)
        assert len(fx.raw_requests) == 2
    assert failure(turn) == 'ruleViolation' and turn.rule_retries_used == 1


@pytest.mark.parametrize('provider', PROVIDERS)
def test_a_decline_and_a_violation_each_consume_one_retry(provider):
    body = rule(action='fix')
    guard = recorder('shelf_check', Verdict(False), Verdict(False), Verdict(True))  # Two declines, then accepted.
    with ConversationFixture(provider) as fx:
        turn = answers(fx, guard, ['Shelf QA-998.', 'Shelf QA-999.', 'Shelf QA-104.'], retries=2, body=body)
        assert len(fx.raw_requests) == 3
    assert turn.result.exit_code == 0 and turn.rule_retries_used == 2
    assert turn.rules_declined == turn.rules_retried == (RULE_ID,) and turn.rules_faulted == ()


def two_call_turn(fx, guard, retries, answer_rule=None):
    calls = []
    fx.tools['shelf_lookup'] = CustomTool(
        'shelf_lookup',
        'Look up a synthetic shelf mark',
        {'type': 'object', 'properties': {'recordId': {'type': 'string'}}, 'additionalProperties': False},
        lambda arguments: calls.append(arguments) or {'shelfMark': 'QA-104'},
        'shelf-v1',
    )
    fx.queue_calls(
        [
            {'id': 'native-1', 'name': 'shelf_lookup', 'arguments': json.dumps({'recordId': 'REC-1'})},
            {'id': 'native-2', 'name': 'shelf_lookup', 'arguments': json.dumps({'recordId': 'REC-2'})},
        ]
    )
    fx.queue_text('Shelf QA-999.')
    fx.queue_text('Shelf QA-104.')
    body = rule('before_tool') + (answer_rule or '')
    return runtime(fx, guard, retries).turn(tagged(body, tools=fx.tools), message='Find both', policy=fx.policy), calls


@pytest.mark.parametrize('provider', PROVIDERS)
@pytest.mark.parametrize('allowance,ok', [(1, False), (2, True)])
def test_one_batch_shares_one_retry_under_the_counter(provider, allowance, ok):
    tool_guard = recorder('shelf_check', Verdict(False), Verdict(False), stages=TOOL_STAGES)
    answer_guard = recorder('tone_check', Verdict(False), Verdict(True))
    answer_rule = '- Be brief. <!-- enforce: tone_check(stage=final, on_violation=retry) -->\n'
    with ConversationFixture(provider) as fx:
        turn, calls = two_call_turn(fx, [tool_guard.guard, answer_guard.guard], allowance, answer_rule)
    assert calls == []  # Both calls of the batch were refused with feedback by one shared retry.
    assert (turn.result.exit_code == 0) is ok and turn.rule_retries_used == allowance


@pytest.mark.parametrize('allowance', [0, 2])
def test_citation_retries_share_the_rule_allowance(allowance):
    with ConversationFixture('openai-compatible') as fx:
        for text in ('missing citation', 'still missing', 'missing again', 'never reached'):
            fx.queue_text(text)
        binding = api.ToolContractBindingsV1(
            'host',
            '1',
            api.InvocationScope('attempt'),
            evidence_authorizer=api.EvidenceAuthorizerV1('sources', '1', lambda use: True),
            citations=api.CitationPolicyV1('required'),
        )
        guard = api.Guard('check', '1', ('final',), lambda stage, ctx: api.Verdict(True))
        agent = tagged('- ALWAYS check. <!-- enforce: check(stage=final, on_violation=retry) -->')
        turn = runtime(fx, [guard], allowance).turn(
            agent, message='Review', policy=fx.policy, tool_contract_bindings=binding
        )
        assert len(fx.requests) == allowance + 1
    assert failure(turn) == 'citationViolation' and turn.rule_retries_used == allowance


@pytest.mark.parametrize('value', [4, -1, True, False, '1', 1.0, None])
def test_invalid_allowances_are_refused_when_the_runtime_is_constructed(value):
    with ConversationFixture('openai-compatible') as fx:
        with pytest.raises(ValueError, match='rule_retries'):
            ProsaicRuntime(fx.runtime.config, rule_retries=value)


@pytest.mark.parametrize('value', [0, 1, 2, 3])
def test_every_allowance_in_range_is_accepted_and_is_not_configuration(value):
    with ConversationFixture('openai-compatible') as fx:
        ProsaicRuntime(fx.runtime.config, rule_retries=value)
    # Host policy, like guards: a RuntimeConfig field would change every Harness workflow identity.
    assert 'rule_retries' not in {field.name for field in fields(RuntimeConfig)}


def test_the_allowance_enters_turn_identity_only_when_not_one():
    guard = recorder('shelf_check')
    with ConversationFixture('openai-compatible') as fx:
        policy = RunPolicy(max_provider_requests=4, max_tool_calls=0)

        def prepared(retries, body=rule()):
            artifact = tagged(body) if body else api.ProsaicArtifact('plain', 'subagent', {}, 'Plain prose.')
            return runtime(fx, [guard.guard], retries).prepare_turn(artifact, message='Where?', policy=policy)

        default, explicit_one, two = prepared(None), prepared(1), prepared(2)
        assert default._encoded == explicit_one._encoded and b'ruleRetries' not in default._encoded
        rules = json.loads(two._encoded)['rules']
        assert rules['ruleRetries'] == 2
        assert rules['fingerprint'] == json.loads(default._encoded)['rules']['fingerprint']
        assert prepared(None, body=None)._encoded == prepared(3, body=None)._encoded  # Untagged turns: unchanged.
        with pytest.raises(TurnAdmissionError) as refused:
            runtime(fx, [guard.guard]).turn(two)
        assert refused.value.code == 'bindingMismatch' and fx.requests == []
