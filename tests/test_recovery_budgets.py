"""A rule retry or tool-request correction still runs under the turn's deadline and token budget.

When either runs out during the extra request, the turn fails with that limit's code (``timeout`` or
``budgetExceeded``), which then stands in for the ``ruleViolation`` or ``invalidToolRequest`` that
prompted the extra request. Corrections behave exactly like rule retries here.
"""

from dataclasses import replace
import time

import pytest

from prosaic_runtime import ProsaicRuntime, Verdict
from support.conversation_server import ConversationFixture
from support.rules_support import recorder, tagged
from test_conversation_tool_batches import queue_json, wire_call

PROVIDERS = ['openai-compatible', 'anthropic']
RETRY_RULE = '- Quote shelf marks exactly. <!-- enforce: shelf_check(stage=final, on_violation=retry) -->\n'


class SlowSecondReply(list):
    """Fixture responses whose second reply arrives only after ``delay`` seconds."""

    def __init__(self, responses, delay):
        super().__init__(responses)
        self.delay, self.served = delay, 0

    def pop(self, index=0):
        self.served += 1
        if self.served == 2:
            time.sleep(self.delay)
        return super().pop(index)


def retry_turn(fx, **limits):
    guard = recorder('shelf_check', Verdict(False), Verdict(True))
    fx.queue_text('Shelf QA-999.')
    fx.queue_text('Shelf QA-104.')
    runtime = ProsaicRuntime(fx.runtime.config, custom_tools=fx.tools, guards=[guard.guard])
    return runtime.turn(tagged(RETRY_RULE), message='Where?', policy=replace(fx.policy, **limits))


def correction_turn(fx, **limits):
    fx.add_tool('lookup', lambda args: {'tide': 'high'})
    queue_json(fx, [wire_call('a', arguments='{"shelf":"QA-999"}')])
    fx.queue_text('High water at noon.')
    runtime = ProsaicRuntime(fx.runtime.config, custom_tools=fx.tools, correct_malformed_tool_calls=True)
    return runtime.turn(fx.agent, message='When is high water?', policy=replace(fx.policy, **limits))


@pytest.mark.parametrize('provider', PROVIDERS)
@pytest.mark.parametrize('run', [retry_turn, correction_turn], ids=['rule-retry', 'correction'])
def test_a_token_budget_spent_by_the_extra_request_fails_with_the_budget_code(provider, run):
    with ConversationFixture(provider) as fx:
        turn = run(fx, max_reported_tokens=6)  # Each fixture reply reports 5 tokens.
        assert len(fx.requests) == 2
    assert turn.evidence.to_dict()['failureCode'] == 'budgetExceeded' and turn.completed_turn is None


@pytest.mark.parametrize('provider', PROVIDERS)
@pytest.mark.parametrize('run', [retry_turn, correction_turn], ids=['rule-retry', 'correction'])
def test_a_deadline_reached_during_the_extra_request_fails_with_the_timeout_code(provider, run):
    with ConversationFixture(provider) as fx:
        fx.responses = SlowSecondReply([], delay=1.6)  # The extra request's reply arrives after the deadline.
        started = time.monotonic()
        turn = run(fx, timeout_s=1.0)
        elapsed = time.monotonic() - started
    assert turn.evidence.to_dict()['failureCode'] == 'timeout' and turn.completed_turn is None
    assert elapsed < 5
