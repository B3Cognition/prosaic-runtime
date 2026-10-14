"""Native framing is fitted without losing source history or splitting tools."""
import json
from dataclasses import replace
import pytest
import prosaic_runtime as api
from support.context_counter import fixture_context
from support.conversation_server import turn_fixture


def history(*texts):
    return tuple(api.ConversationMessage.from_dict({'role': 'user',
        'content': [{'type': 'text', 'text': text}], 'turnStatus': 'failed',
        'failureCode': 'providerFailure'}) for text in texts)


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_actual_full_history_can_fit_nonmonotonic_counter(turn_fixture, provider):
    policy, counters = fixture_context(provider, mode='omitOldestTurns', capacity=200,
        reserve=20, count=lambda request: 10 if 'retained-marker' in json.dumps(request) else 1000)
    with turn_fixture(provider) as fx:
        prepared = fx.runtime.prepare_turn(fx.agent, message='now',
            history=history('retained-marker'), policy=fx.policy,
            context_policy=policy, token_counters=counters)
        assert prepared.context_report.omitted_turn_count == 0
        assert prepared.encoding_version == 'conversation-turn-v2'
        assert fx.requests == []


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_omission_selects_first_fitting_whole_suffix_and_preserves_source(turn_fixture, provider):
    counted = []
    def count(request):
        counted.append(request)
        return 1000 if 'old-marker' in json.dumps(request) else 10
    policy, counters = fixture_context(provider, mode='omitOldestTurns',
        capacity=200, reserve=20, count=count)
    source = history('old-marker', 'recent-marker')
    with turn_fixture(provider) as fx:
        prepared = fx.runtime.prepare_turn(fx.agent, message='current-marker',
            history=source, policy=fx.policy, context_policy=policy, token_counters=counters)
        report = prepared.context_report.to_dict()
        assert report['omittedTurnCount'] == report['retainedTurnCount'] == 1
        assert report['omittedRange'] == {'startTurn': 1, 'endTurnExclusive': 2}
        assert len(counted) == 2
        assert 'old-marker' not in json.dumps(counted[-1])
        assert 'recent-marker' in json.dumps(counted[-1])
        material = json.loads(prepared._encoded)
        assert len(material['history']) == 2
        assert source[0].to_dict()['content'][0]['text'] == 'old-marker'
        assert 'current-marker' not in json.dumps(report)


@pytest.mark.parametrize('tokens,accepted', [(180, True), (181, False)])
def test_exact_token_capacity_boundary(turn_fixture, tokens, accepted):
    policy, counters = fixture_context(capacity=200, reserve=20, count=lambda request: tokens)
    with turn_fixture('openai-compatible') as fx:
        if accepted:
            prepared = fx.runtime.prepare_turn(fx.agent, message='x', policy=fx.policy,
                context_policy=policy, token_counters=counters)
            assert prepared.context_report.input_tokens == 180
        else:
            with pytest.raises(api.TurnAdmissionError) as caught:
                fx.runtime.prepare_turn(fx.agent, message='x', policy=fx.policy,
                    context_policy=policy, token_counters=counters)
            assert caught.value.code == 'contextLimit'
        assert fx.requests == []


def test_final_byte_limit_can_be_rescued_without_bypassing_source_limit(turn_fixture):
    counts = []
    policy, counters = fixture_context(mode='omitOldestTurns', count=lambda request:
        counts.append(request) or 1)
    with turn_fixture('openai-compatible') as fx:
        limits = api.TurnLimits(max_input_bytes=600)
        prepared = fx.runtime.prepare_turn(fx.agent, message='ž', policy=fx.policy,
            history=history('old'*1000), limits=limits,
            context_policy=policy, token_counters=counters)
        assert prepared.input_bytes <= 600
        assert prepared.context_report.omitted_turn_count == 1
        assert len(counts) == 1  # oversized candidate never enters counter
        counts.clear()
        with pytest.raises(api.TurnAdmissionError) as caught:
            fx.runtime.prepare_turn(fx.agent, message='ž', policy=fx.policy,
                history=history('old'*1000), limits=replace(limits, max_history_bytes=100),
                context_policy=policy, token_counters=counters)
        assert caught.value.code == 'contextLimit' or caught.value.code == 'invalidHistory'
        assert counts == []


@pytest.mark.parametrize('checks,bytes_cap', [(2, 33_554_432), (128, 1)])
def test_work_allowance_reserves_readmission_before_returning_preparation(turn_fixture, checks, bytes_cap):
    policy, counters = fixture_context(mode='omitOldestTurns', capacity=200, reserve=20,
        checks=checks, byte_allowance=bytes_cap,
        count=lambda request: 1000 if 'old-marker' in json.dumps(request) else 1)
    with turn_fixture('openai-compatible') as fx:
        with pytest.raises(api.TurnAdmissionError) as caught:
            fx.runtime.prepare_turn(fx.agent, message='now', history=history('old-marker'),
                policy=fx.policy, context_policy=policy, token_counters=counters)
        assert caught.value.code == 'contextLimit'
        assert fx.requests == []


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_output_reservation_is_enforced_in_the_counted_native_payload(turn_fixture, provider):
    seen = []
    policy, counters = fixture_context(provider, reserve=37,
        count=lambda request: seen.append(request) or 1)
    with turn_fixture(provider) as fx:
        prepared = fx.runtime.prepare_turn(fx.agent, message='ž', policy=fx.policy,
            context_policy=policy, token_counters=counters)
        assert seen[-1]['max_tokens'] == 37
        assert prepared.input_bytes == len(json.dumps(seen[-1], allow_nan=False).encode('utf-8'))


def test_legacy_preparation_identity_is_unchanged(turn_fixture):
    with turn_fixture('openai-compatible') as fx:
        first = fx.runtime.prepare_turn(fx.agent, message='same', policy=fx.policy)
        second = fx.runtime.prepare_turn(fx.agent, message='same', policy=fx.policy,
            context_policy=None, token_counters=None)
        assert first._encoded == second._encoded
        assert first.context_identity == second.context_identity
        assert first.encoding_version == 'conversation-turn-v1'
        assert first.context_report is None


def test_frozen_selection_reconstruction_does_not_repeat_search(turn_fixture):
    counts = []
    policy, counters = fixture_context(mode='omitOldestTurns', capacity=200, reserve=20,
        count=lambda request: counts.append(request) or
            (1000 if 'old-marker' in json.dumps(request) else 1))
    with turn_fixture('openai-compatible') as fx:
        kwargs = dict(message='now', history=history('old-marker', 'recent'),
            policy=fx.policy, context_policy=policy, token_counters=counters)
        prepared = fx.runtime.prepare_turn(fx.agent, **kwargs)
        replay = fx.runtime.prepare_turn(fx.agent, **kwargs,
            context_selection=prepared.context_selection,
            context_report=prepared.context_report, context_work=prepared.context_work)
        assert len(counts) == 2
        assert replay.context_identity == prepared.context_identity
        assert replay._encoded == prepared._encoded
        with pytest.raises(api.TurnAdmissionError):
            fx.runtime.prepare_turn(fx.agent, **{**kwargs, 'history': history('changed')},
                context_selection=prepared.context_selection,
                context_report=prepared.context_report, context_work=prepared.context_work)


def test_active_time_is_cooperative_and_charged_for_construction():
    from prosaic_runtime.context_work import ContextWorkBudget
    policy, _ = fixture_context(timeout_ms=1)
    now = [0]
    budget = ContextWorkBudget(policy, clock=lambda: now[0])
    def slow():
        now[0] = 1_000_001
        return {}
    with pytest.raises(api.TurnAdmissionError) as caught:
        budget.construct(slow)
    assert caught.value.limit == 'assemblyTimeoutMs'


def test_tool_exchange_stays_with_its_initiating_turn(turn_fixture):
    source = tuple(api.ConversationMessage.from_dict(m) for m in [
        {'role': 'user', 'content': [{'type': 'text', 'text': 'old-marker'}], 'turnStatus': 'completed'},
        {'role': 'assistant', 'content': [{'type': 'toolCall', 'callId': 'one', 'name': 'lookup', 'arguments': {}}]},
        {'role': 'tool', 'content': [{'type': 'toolOutcome', 'callId': 'one', 'outcome': {'status': 'ok', 'result': {}}}]},
        {'role': 'assistant', 'content': [{'type': 'text', 'text': 'old-answer'}]},
        history('recent-marker')[0].to_dict()])
    policy, counters = fixture_context(mode='omitOldestTurns', capacity=200, reserve=20,
        count=lambda request: 1000 if 'old-marker' in json.dumps(request) else 1)
    with turn_fixture('openai-compatible') as fx:
        prepared = fx.runtime.prepare_turn(fx.agent, message='now', history=source,
            policy=fx.policy, context_policy=policy, token_counters=counters)
        selected = json.loads(prepared._encoded)['selectedHistory']
        assert len(selected) == 1
        assert selected[0]['content'][0]['text'] == 'recent-marker'
