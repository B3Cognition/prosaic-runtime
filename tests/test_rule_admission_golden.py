"""Headline: no tag, no behaviour. Untagged agents match bytes captured before rule admission."""

import pytest

from prosaic_runtime import generic_guards
from support.rules_support import runtime_for
from support.untagged_golden import GOLDEN, capture, load


CONFIGURATIONS = {
    'no-guards': lambda fx: runtime_for(fx),
    'generic-guards-registered': lambda fx: runtime_for(fx, guards=generic_guards()),
    'allow-unbound': lambda fx: runtime_for(fx, guards=generic_guards(), allow_unbound_guards=True),
    'explicit-empty-required': lambda fx: runtime_for(fx, required_guards={}),
}


@pytest.mark.parametrize('configuration', sorted(CONFIGURATIONS))
def test_untagged_agents_are_byte_identical_to_pre_rule_admission_main(configuration):
    golden = load()
    observed = capture(CONFIGURATIONS[configuration])
    assert sorted(observed) == sorted(golden)
    for name in golden:
        assert observed[name]['requests'] == golden[name]['requests'], f'{name}: request bytes changed'
        assert observed[name] == golden[name], f'{name}: observable result changed'


def test_untagged_turn_result_reports_no_rules():
    from support.conversation_server import ConversationFixture
    from support.untagged_golden import plain_agent

    with ConversationFixture('openai-compatible') as fx:
        fx.queue_text('Answer')
        turn = runtime_for(fx, guards=generic_guards()).turn(plain_agent(), message='Hi', policy=fx.policy)
    assert (turn.rules_evaluated, turn.rules_violated, turn.rules_fixed, turn.rules_fingerprint) == ((), (), (), None)


def test_golden_file_is_a_reviewed_fixture():
    text = GOLDEN.read_text(encoding='utf-8')
    assert '"review"' in text and 'requires re-review' in text
