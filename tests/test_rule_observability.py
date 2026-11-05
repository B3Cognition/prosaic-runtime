"""Rule observability: guard faults, guard time, enforcement coverage and host-only configuration."""

import time

import pytest
from prosaic import parse_rules

from prosaic_runtime import Guard, ProsaicArtifact, RunPolicy, Verdict, compile_rules, generic_guards
from support.conversation_server import ConversationFixture
from support.rules_support import recorder, runtime_for, tagged

PROVIDERS = ['openai-compatible', 'anthropic']
RULE = 'subagents/shelf-keeper.md:1:shelf_check'


def rule(action='retry'):
    return f'- Keep entries short. <!-- enforce: shelf_check(stage=final, on_violation={action}) -->\n'


@pytest.mark.parametrize('provider', PROVIDERS)
@pytest.mark.parametrize(
    'action,verdict',
    [('retry', RuntimeError('guard bug')), ('fix', Verdict(False, fixed=7)), ('retry', 'not a verdict')],
)
def test_a_guard_fault_is_reported_apart_from_a_rule_violation(provider, action, verdict):
    with ConversationFixture(provider) as fx:
        fx.queue_text('draft')
        turn = runtime_for(fx, guards=[recorder('shelf_check', verdict).guard]).turn(
            tagged(rule(action)), message='Go', policy=fx.policy
        )
    assert turn.evidence.to_dict()['failureCode'] == 'ruleViolation'
    assert turn.rules_faulted == (RULE,)


@pytest.mark.parametrize('provider', PROVIDERS)
def test_a_real_violation_is_not_a_fault(provider):
    with ConversationFixture(provider) as fx:
        fx.queue_text('draft')
        turn = runtime_for(fx, guards=[recorder('shelf_check', Verdict(False)).guard]).turn(
            tagged(rule('fail')), message='Go', policy=fx.policy
        )
    assert turn.rules_violated == (RULE,) and turn.rules_faulted == ()


@pytest.mark.parametrize('provider', PROVIDERS)
def test_a_slow_guard_ends_the_turn_at_the_deadline_as_soon_as_it_returns(provider):
    def slow(stage, context):
        time.sleep(1.2)
        return Verdict(True)

    with ConversationFixture(provider) as fx:
        fx.queue_text('draft')
        started = time.monotonic()
        turn = runtime_for(fx, guards=[Guard('shelf_check', 'v1', ('final',), slow)]).turn(
            tagged(rule()), message='Go', policy=RunPolicy(timeout_s=0.8, max_provider_requests=4, max_tool_calls=0)
        )
        elapsed = time.monotonic() - started
    assert turn.evidence.to_dict()['failureCode'] == 'timeout' and turn.completed_turn is None
    assert elapsed < 3


def test_coverage_reports_enforced_unbound_and_prose_only_rules_by_id():
    body = (
        '- Keep entries short. <!-- enforce: shelf_check(on_violation=retry) -->\n'
        '- Quote figures. <!-- enforce: figure_check(on_violation=retry) -->\n'
        '- Be warm. <!-- prose-only: tone is judged by people -->\n'
    )
    artifact = tagged(body)
    with ConversationFixture('openai-compatible') as fx:
        runtime = runtime_for(fx, guards=[recorder('shelf_check').guard])
        assert runtime.rule_coverage(artifact) == {
            'registered': ['subagents/shelf-keeper.md:1:shelf_check'],
            'unregistered': ['subagents/shelf-keeper.md:2:figure_check'],
            'proseOnly': ['subagents/shelf-keeper.md:3:prose-only'],
            'issues': 0,
        }
        assert fx.raw_requests == []
    compiled = compile_rules(parse_rules(body, 'a.md')['rules'], [recorder('shelf_check').guard], allow_unbound=True)
    assert compiled.coverage() == {
        'enforced': ['a.md:1:shelf_check'],
        'unbound': ['a.md:2:figure_check'],
        'proseOnly': ['a.md:3:prose-only'],
    }


def test_coverage_counts_issues_without_refusing_and_never_exposes_text():
    body = '- Never leak. <!-- enforce: shelf_check(on_violation=fail -->\n'
    with ConversationFixture('openai-compatible') as fx:
        report = runtime_for(fx).rule_coverage(tagged(body))
    assert report['issues'] == 1 and 'Never leak' not in str(report)
    assert runtime_for_plain_coverage() == {'registered': [], 'unregistered': [], 'proseOnly': [], 'issues': 0}


def runtime_for_plain_coverage():
    with ConversationFixture('openai-compatible') as fx:
        return runtime_for(fx).rule_coverage(ProsaicArtifact('plain', 'subagent', {}, 'Plain prose.'))


def test_unregistered_required_guard_message_explains_host_only_configuration():
    with ConversationFixture('openai-compatible') as fx:
        with pytest.raises(ValueError, match='need a Python host that registers guards'):
            runtime_for(fx, required_guards={'strict_json': 'fail'})


def test_only_value_in_tool_output_echoes_values_in_answer_feedback():
    assert {guard.name: guard.echo_values for guard in generic_guards()} == {
        'no_wrapped_links': False,
        'value_in_tool_output': True,
        'args_reference_seen_ids': False,
        'strict_json': False,
    }
    with pytest.raises(ValueError):
        Guard('shelf_check', 'v1', ('final',), lambda stage, context: Verdict(True), echo_values='yes')
