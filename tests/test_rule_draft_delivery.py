"""A draft that answer-stage rules may refuse is never streamed, previewed or delivered early."""

import contextlib
import io
import json

import pytest

from prosaic_runtime import Verdict
from support.conversation_server import ConversationFixture
from support.rules_support import recorder, runtime_for, tagged

PROVIDERS = ['openai-compatible', 'anthropic']
DRAFT, ACCEPTED = 'Call the private line SECRET-DRAFT now.', 'Shelf QA-104.'


def rule(stage, action):
    return f'- Share no contact details. <!-- enforce: shelf_check(stage={stage}, on_violation={action}) -->\n'


def run(fx, body, guard, *, answers, stream):
    for answer in answers:
        (fx.queue_stream_text if stream else fx.queue_text)(answer)
    events, stderr = [], io.StringIO()
    with contextlib.redirect_stderr(stderr):
        turn = runtime_for(fx, guards=[guard.guard] if guard else []).turn(
            tagged(body), message='Go', policy=fx.policy, on_event=events.append
        )
    return turn, json.dumps(events, ensure_ascii=False), stderr.getvalue()


def text_events(events):
    return [e['text'] for e in json.loads(events) if e['event'] in ('text', 'text_delta')]


@pytest.mark.parametrize('provider', PROVIDERS)
@pytest.mark.parametrize('stream', [True, False])
def test_a_refused_draft_never_reaches_observers_or_the_console_and_the_accepted_answer_does(provider, stream):
    guard = recorder('shelf_check', Verdict(False, refused_values=('private line',)))
    with ConversationFixture(provider) as fx:
        turn, events, stderr = run(fx, rule('final', 'retry'), guard, answers=[DRAFT, ACCEPTED], stream=stream)
    assert turn.result.stdout == ACCEPTED
    assert 'SECRET-DRAFT' not in events and 'SECRET-DRAFT' not in stderr
    if stream:
        assert text_events(events) == [ACCEPTED]  # Delivered once, after every answer rule accepted it.


@pytest.mark.parametrize('provider', PROVIDERS)
@pytest.mark.parametrize('stream', [True, False])
def test_a_failed_draft_is_never_delivered(provider, stream):
    guard = recorder('shelf_check', Verdict(False))
    with ConversationFixture(provider) as fx:
        turn, events, stderr = run(fx, rule('before_answer', 'fail'), guard, answers=[DRAFT], stream=stream)
    assert turn.evidence.to_dict()['failureCode'] == 'ruleViolation'
    assert 'SECRET-DRAFT' not in events and 'SECRET-DRAFT' not in stderr and text_events(events) == []


@pytest.mark.parametrize('provider', PROVIDERS)
def test_a_repaired_draft_is_delivered_only_in_its_repaired_form(provider):
    guard = recorder('shelf_check', Verdict(False, fixed=ACCEPTED), Verdict(True))
    with ConversationFixture(provider) as fx:
        turn, events, _ = run(fx, rule('final', 'fix'), guard, answers=[DRAFT], stream=True)
    assert turn.result.stdout == ACCEPTED and text_events(events) == [ACCEPTED]


@pytest.mark.parametrize('provider', PROVIDERS)
def test_text_streams_live_without_answer_stage_rules(provider):
    tool_rule = '- Use known shelves. <!-- enforce: shelf_check(stage=before_tool, on_violation=fail) -->\n'
    with ConversationFixture(provider) as fx:
        _, untagged, _ = run(fx, 'Plain prose.', None, answers=[DRAFT], stream=True)
    with ConversationFixture(provider) as fx:
        guard = recorder('shelf_check', stages=('before_tool',))
        _, tool_only, _ = run(fx, tool_rule, guard, answers=[DRAFT], stream=True)
    assert text_events(untagged) == [DRAFT] and text_events(tool_only) == [DRAFT]
