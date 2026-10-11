"""Full-history turns enforce actual wire and retained-record boundaries."""
from dataclasses import replace
import json

import pytest
from prosaic_runtime import CustomTool, TurnAdmissionError, TurnLimits
from support.conversation_server import turn_fixture


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_turn_preserves_history_large_enough_to_activate_existing_compactor(turn_fixture, provider):
    with turn_fixture(provider) as fx:
        history = fx.compaction_history(outcomes=9, older_result_chars=100_000)
        before = json.dumps([m.to_dict() for m in history])
        fx.queue_text('done')
        result = fx.runtime.turn(fx.agent, message='Continue', history=history,
                                 policy=fx.large_policy, limits=fx.large_limits)
        assert result.result.exit_code == 0
        assert 'x' * 100_000 in json.dumps(fx.requests[0])
        assert json.dumps([m.to_dict() for m in history]) == before


def test_legacy_run_still_compacts_the_same_large_result_fixture(turn_fixture, tmp_path):
    with turn_fixture('openai-compatible') as fx:
        effects = []
        def handler(args):
            effects.append(1)
            return {'content': 'x' * 100_000 if len(effects) == 1 else 'small'}
        fx.tools['lookup'] = CustomTool('lookup', 'Lookup', {'type': 'object', 'additionalProperties': False},
                                       handler, 'v1', max_result_bytes=262_144)
        fx.queue_calls([{'id': f'call_{i}', 'name': 'lookup', 'arguments': '{}'} for i in range(9)])
        fx.queue_text('done')
        result = fx.runtime.run(fx.agent, cwd=tmp_path, policy=fx.large_policy)
        assert result.exit_code == 0 and effects == [1] * 9
        assert result.metadata['tool_result_compactions'] == 1
        assert 'x' * 100_000 not in json.dumps(fx.requests[1])
        assert 'compacted' in json.dumps(fx.requests[1])


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_exact_first_wire_boundary_is_accepted_and_stricter_policy_rejects_without_post(turn_fixture, provider):
    with turn_fixture(provider) as fx:
        fx.add_tool('lookup', lambda args: {})
        rt = fx.runtime
        prepared = rt.prepare_turn(fx.agent, message='Příliš "\\"', policy=fx.policy)
        fx.queue_text('done')
        result = rt.turn(fx.agent, message='Příliš "\\"',
                         policy=replace(fx.policy, max_input_bytes=prepared.input_bytes))
        assert result.result.exit_code == 0
        assert len(fx.raw_requests[0]) == prepared.input_bytes
        with pytest.raises(TurnAdmissionError) as exc:
            rt.turn(fx.agent, message='Příliš "\\"',
                    policy=replace(fx.policy, max_input_bytes=prepared.input_bytes-1))
        assert (exc.value.code, exc.value.limit) == ('contextLimit', 'maxInputBytes')
        assert len(fx.requests) == 1


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_post_tool_overflow_keeps_known_effect_and_issues_no_followup(turn_fixture, provider):
    with turn_fixture(provider) as fx:
        effects = []
        fx.add_tool('lookup', lambda args: effects.append(1) or {'content': 'x' * 2000})
        rt = fx.runtime
        prepared = rt.prepare_turn(fx.agent, message='Search', policy=fx.policy)
        fx.queue_calls([{'id': 'call_0', 'name': 'lookup', 'arguments': '{}'}])
        fx.queue_text('must not be reached')
        result = rt.turn(fx.agent, message='Search',
                         policy=replace(fx.policy, max_input_bytes=prepared.input_bytes + 500))
        assert effects == [1] and len(fx.requests) == 1
        assert result.result.metadata['failure_reason'] == 'contextLimit'
        evidence = result.evidence.to_dict()
        assert evidence['knownUsage']['totalTokens'] == 5 and evidence['usageComplete'] is True
        assert evidence['toolOutcomes'][0]['status'] == 'completed'
        assert result.completed_turn is None


@pytest.mark.parametrize('limits', [TurnLimits(max_assistant_bytes=3), TurnLimits(max_turn_bytes=80)])
def test_assistant_and_complete_record_caps_are_independent(turn_fixture, limits):
    with turn_fixture('openai-compatible') as fx:
        fx.queue_text('žž')
        result = fx.runtime.turn(fx.agent, message='Hello', policy=fx.policy, limits=limits)
        assert result.result.exit_code != 0 and result.result.metadata['failure_reason'] == 'outputLimit'
        assert result.completed_turn is None
        assert result.evidence.to_dict()['knownUsage']['totalTokens'] == 5


def test_provider_window_rejection_never_retries_with_shrunk_history(turn_fixture):
    with turn_fixture('openai-compatible') as fx:
        fx.queue_http_failure(400)
        history = fx.compaction_history()
        result = fx.runtime.turn(fx.agent, message='Continue', history=history,
                                 policy=fx.large_policy, limits=fx.large_limits)
        assert result.completed_turn is None and len(fx.requests) == 1
        assert 'x' * 100_000 in json.dumps(fx.requests[0])
