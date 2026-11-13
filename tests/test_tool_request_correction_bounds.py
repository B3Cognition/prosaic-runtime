"""Where the one tool-request correction may not loosen the turn: a host-required first tool and tool shutdown."""

from dataclasses import replace
import json

import pytest

from prosaic_runtime import ProsaicRuntime
from prosaic_runtime.tool_request_correction import PREFIX
from support.conversation_server import ConversationFixture
from test_conversation_tool_batches import queue_json, wire_call

PROVIDERS = ['openai-compatible', 'anthropic']
SCHEMA_INVALID = wire_call('a', arguments='{"shelf":"QA-999"}')  # The tool schema admits no properties.


def corrected(fx, *, enabled=True):
    return ProsaicRuntime(fx.runtime.config, custom_tools=fx.tools, correct_malformed_tool_calls=enabled)


def lookup_fixture(fx):
    effects = []
    fx.add_tool('lookup', lambda args: effects.append(args) or {'tide': 'high'})
    return effects


def required(fx):
    return replace(fx.policy, initial_tool='lookup')


def request(fx, index):
    return json.loads(fx.raw_requests[index])


def envelope_prefix(fx, index):
    message = request(fx, index)['messages'][-1]
    content = message['content'] if isinstance(message['content'], str) else message['content'][-1]['text']
    assert 'toolRequestCorrection' in content
    return content.split('\n', 1)[0] + '\n'


def failure(turn):
    return turn.evidence.to_dict()['failureCode']


@pytest.mark.parametrize('provider', PROVIDERS)
def test_a_required_first_tool_stays_forced_and_the_envelope_asks_only_to_resend(provider):
    with ConversationFixture(provider) as fx:
        effects = lookup_fixture(fx)
        queue_json(fx, [SCHEMA_INVALID])
        queue_json(fx, [wire_call('c')])
        fx.queue_text('High water at noon.')
        turn = corrected(fx).turn(fx.agent, message='When is high water?', policy=required(fx))
        assert len(fx.requests) == 3
        first, correction = request(fx, 0), request(fx, 1)
        prefix = envelope_prefix(fx, 1)
    # The correction request carries the same forcing as the original request, on its own wire.
    forced = (
        {'type': 'function', 'function': {'name': 'lookup'}}
        if provider == 'openai-compatible'
        else {'type': 'tool', 'name': 'lookup', 'disable_parallel_tool_use': True}
    )
    assert first['tool_choice'] == correction['tool_choice'] == forced
    assert [t.get('name') or t['function']['name'] for t in correction['tools']] == ['lookup']
    # Only a resend with valid arguments is asked for; answering without the tool is not offered.
    assert 'without it' not in prefix and 'valid arguments' in prefix and prefix != PREFIX
    assert turn.result.exit_code == 0 and effects == [{}]
    assert turn.tool_request_corrections == ('argumentsSchema',)


@pytest.mark.parametrize('provider', PROVIDERS)
def test_a_text_only_answer_while_the_required_tool_is_unhonoured_fails_as_without_the_correction(provider):
    with ConversationFixture(provider) as fx:
        effects = lookup_fixture(fx)
        queue_json(fx, [SCHEMA_INVALID])
        fx.queue_text('High water at noon.')  # Ungrounded: the required lookup never ran.
        turn = corrected(fx).turn(fx.agent, message='When is high water?', policy=required(fx))
        assert len(fx.requests) == 2
    with ConversationFixture(provider) as control:
        lookup_fixture(control)
        control.queue_text('High water at noon.')  # The same text-only answer to the forced request, no correction.
        baseline = control.runtime.turn(control.agent, message='When is high water?', policy=required(control))
    assert effects == [] and turn.result.exit_code == 1 and turn.completed_turn is None
    assert 'High water' not in turn.result.stdout
    assert failure(turn) == failure(baseline) == 'providerFailure'
    assert turn.result.stderr == baseline.result.stderr


@pytest.mark.parametrize('provider', PROVIDERS)
def test_an_optional_tool_keeps_the_offer_to_answer_without_it(provider):
    with ConversationFixture(provider) as fx:
        effects = lookup_fixture(fx)
        queue_json(fx, [SCHEMA_INVALID])
        fx.queue_text('High water at noon.')
        turn = corrected(fx).turn(fx.agent, message='When is high water?', policy=fx.policy)
        assert len(fx.requests) == 2
        correction = request(fx, 1)
        prefix = envelope_prefix(fx, 1)
    assert prefix == PREFIX and 'or answer without it' in prefix
    assert correction['tool_choice'] in ('auto', {'type': 'auto'})
    # Unchanged by design for an optional tool: grounding is the host's acceptance check, not the correction's.
    assert turn.result.exit_code == 0 and turn.result.stdout == 'High water at noon.' and effects == []


def shut_down_then_malformed(fx):
    """Three identical rounds disable tools for lack of progress; the model then sends a malformed batch."""
    for index in range(3):
        queue_json(fx, [wire_call(f'r{index}')])
    queue_json(fx, [SCHEMA_INVALID | {'id': 'm'}])
    fx.queue_text('Should not be reached')


@pytest.mark.parametrize('provider', PROVIDERS)
def test_no_correction_runs_after_tools_are_disabled_for_lack_of_progress(provider):
    with ConversationFixture(provider) as fx:
        effects = lookup_fixture(fx)
        shut_down_then_malformed(fx)
        turn = corrected(fx).turn(fx.agent, message='When is high water?', policy=fx.policy)
        sent = len(fx.requests)
        shutdown = request(fx, 3)
    with ConversationFixture(provider) as control:
        lookup_fixture(control)
        shut_down_then_malformed(control)
        baseline = corrected(control, enabled=False).turn(
            control.agent, message='When is high water?', policy=control.policy
        )
        expected = len(control.requests)
    assert not shutdown.get('tools')  # The fourth request is the post-shutdown one.
    # Zero additional provider requests: the stop path, exactly as with the correction off.
    assert sent == expected == 4 and effects == [{}, {}, {}]
    assert turn.evidence.to_dict()['providerAttempts'] == baseline.evidence.to_dict()['providerAttempts'] == 4
    assert failure(turn) == failure(baseline) == 'invalidToolRequest'
    assert turn.tool_request_corrections == () and turn.completed_turn is None
