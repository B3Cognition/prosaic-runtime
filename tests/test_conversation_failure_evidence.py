"""Known effects and observed usage survive every failure boundary."""
from dataclasses import asdict, replace
import json
import time

import pytest
from prosaic_runtime import (CustomTool, ExecutionContext, InvocationScope, TurnAdmissionError, TurnLimits)
from prosaic_runtime.accounting import MemoryRecorder
from support.conversation_server import turn_fixture
from support.tool_journal import SqliteJournal


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_post_journal_commit_cancellation_retains_known_completion(turn_fixture, provider):
    with turn_fixture(provider) as fx:
        fx.cancel_after_journal_commit()
        result = fx.run_journaled_turn()
        evidence = result.evidence.to_dict()
        assert fx.handler_calls == 1 and result.completed_turn is None
        assert result.result.metadata['failure_reason'] == 'cancelled'
        assert evidence['toolOutcomes'][0]['status'] == 'completed'
        assert evidence['toolOutcomes'][0]['outcome']['result'] == {'completed': True}
        assert len(fx.requests) == 1


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
@pytest.mark.parametrize('failure', ['prepare', 'observe'])
def test_accounting_failure_preserves_actual_dispatch_and_usage(turn_fixture, provider, failure):
    class Recorder(MemoryRecorder):
        def prepare(self, intent):
            if failure == 'prepare':
                raise RuntimeError('private prepare failure')
            super().prepare(intent)
        def observe(self, call_id, observation):
            raise RuntimeError('private observation failure')
    with turn_fixture(provider) as fx:
        fx.queue_text('done')
        result = fx.runtime.turn(fx.agent, message='Hello', policy=fx.policy,
            accounting=Recorder(provider_id=provider), context=ExecutionContext(
                application_id='app', tenant_id='owner', billing_account_id='payer',
                request_id='request', invocation_id='attempt'))
        evidence = result.evidence.to_dict()
        assert result.result.metadata['failure_reason'] == 'accountingFailure'
        assert result.completed_turn is None and 'private' not in json.dumps(asdict(result.result))
        assert evidence['accounting']['context']['tenant_id'] == 'owner'
        if failure == 'observe':
            assert len(fx.requests) == evidence['providerAttempts'] == 1
            assert evidence['knownUsage']['totalTokens'] == result.result.token_usage == 5
            assert evidence['usageComplete'] is True
        else:
            assert fx.requests == [] and evidence['providerAttempts'] == 0
            assert evidence['knownUsage'] == {} and evidence['usageComplete'] is True


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_unknown_second_provider_call_keeps_prior_known_usage(turn_fixture, provider):
    with turn_fixture(provider) as fx:
        fx.add_tool('lookup', lambda args: {})
        fx.queue_calls([{'id': 'call_0', 'name': 'lookup', 'arguments': '{}'}])
        fx.queue_http_failure(503)
        result = fx.runtime.turn(fx.agent, message='Find', policy=fx.policy)
        evidence = result.evidence.to_dict()
        assert evidence['knownUsage']['totalTokens'] == 5
        assert evidence['usageComplete'] is False and result.result.token_usage is None
        assert evidence['providerAttempts'] == 2 and evidence['toolOutcomes'][0]['status'] == 'completed'


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_post_plain_handler_cancellation_does_not_erase_completed_call(turn_fixture, provider):
    with turn_fixture(provider) as fx:
        cancelled, effects = [], []
        def handler(args):
            effects.append(1)
            cancelled.append(True)
            return {'completed': True}
        fx.add_tool('lookup', handler)
        fx.queue_calls([{'id': 'a', 'name': 'lookup', 'arguments': '{}'}])
        result = fx.runtime.turn(fx.agent, message='Find', policy=fx.policy, cancelled=lambda: bool(cancelled))
        assert effects == [1] and result.result.metadata['failure_reason'] == 'cancelled'
        assert result.evidence.to_dict()['toolOutcomes'][0]['status'] == 'completed'


def test_post_handler_deadline_retains_completed_call(turn_fixture):
    with turn_fixture('openai-compatible') as fx:
        effects = []
        def handler(args):
            effects.append(1)
            time.sleep(0.08)
            return {'completed': True}
        fx.add_tool('lookup', handler)
        fx.queue_calls([{'id': 'a', 'name': 'lookup', 'arguments': '{}'}])
        result = fx.runtime.turn(fx.agent, message='Find', policy=replace(fx.policy, timeout_s=.05))
        assert effects == [1] and result.result.metadata['failure_reason'] == 'timeout'
        assert result.evidence.to_dict()['toolOutcomes'][0]['status'] == 'completed'


@pytest.mark.parametrize('lost_ack', [False, True])
def test_uncertain_or_lost_journal_commit_is_never_reported_completed(turn_fixture, tmp_path, lost_ack):
    class Journal(SqliteJournal):
        def commit(self, *args):
            super().commit(*args)
            raise RuntimeError('private lost acknowledgement')
    with turn_fixture('openai-compatible') as fx:
        effects = []
        fx.add_tool('lookup', lambda args, context: effects.append(1) or {},
                    operation_key=lambda args, context: 'effect')
        journal = Journal(tmp_path / 'journal.sqlite') if lost_ack else SqliteJournal(tmp_path / 'journal.sqlite')
        if not lost_ack:
            # An existing unresolved claim is authoritative uncertainty.
            import hashlib
            signature = hashlib.sha256(b'{"arguments":{},"tool_name":"lookup","tool_version":"v1"}').hexdigest()
            journal.claim('fixture-ops', 'effect', signature)
        fx.queue_calls([{'id': 'a', 'name': 'lookup', 'arguments': '{}'}])
        result = fx.runtime.turn(fx.agent, message='Find', policy=fx.policy, tool_journal=journal,
            operation_context=InvocationScope('attempt', operation_namespace='fixture-ops'))
        assert effects == ([1] if lost_ack else []) and len(fx.requests) == 1
        assert result.completed_turn is None
        assert result.evidence.to_dict()['toolOutcomes'][0]['status'] == 'uncertain'
        assert result.result.metadata['failure_reason'] == ('toolJournalFailure' if lost_ack else 'toolEffectUncertain')


def test_large_effect_body_falls_back_to_bounded_known_status_before_followup(turn_fixture):
    with turn_fixture('openai-compatible') as fx:
        effects = []
        fx.add_tool('lookup', lambda args: effects.append(1) or {'content': 'x' * 40_000})
        fx.queue_calls([{'id': 'a', 'name': 'lookup', 'arguments': '{}'}])
        fx.queue_text('Should not be reached')
        result = fx.runtime.turn(fx.agent, message='Find', policy=replace(fx.policy, max_tool_calls=1),
                                 limits=TurnLimits(max_evidence_bytes=4096))
        evidence = result.evidence.to_dict()
        assert effects == [1] and len(fx.requests) == 1
        assert result.completed_turn is None and result.result.metadata['failure_reason'] == 'outputLimit'
        assert len(json.dumps(evidence, ensure_ascii=False, separators=(',', ':')).encode()) <= 4096
        assert evidence['toolOutcomes'][0]['status'] == 'completed' and 'outcome' not in evidence['toolOutcomes'][0]


def test_evidence_summary_space_is_required_before_dispatch(turn_fixture):
    with turn_fixture('openai-compatible') as fx:
        with pytest.raises(TurnAdmissionError) as exc:
            fx.runtime.turn(fx.agent, message='Hi', policy=fx.policy, limits=TurnLimits(max_evidence_bytes=8))
        assert exc.value.code == 'invalidPolicy' and fx.requests == []


def test_full_length_operation_scope_produces_bounded_neutral_ids(turn_fixture):
    with turn_fixture('openai-compatible') as fx:
        fx.add_tool('lookup', lambda args: {})
        fx.queue_calls([{'id': 'private-native', 'name': 'lookup', 'arguments': '{}'}])
        fx.queue_text('done')
        result = fx.runtime.turn(fx.agent, message='Find', policy=fx.policy,
                                 operation_context=InvocationScope('a' * 128))
        assert result.result.exit_code == 0
        call_id = result.completed_turn.to_dict()['messages'][1]['content'][1]['callId']
        assert len(call_id) <= 128


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_callback_failure_after_effect_preserves_evidence_and_neutral_event_id(turn_fixture, provider):
    with turn_fixture(provider) as fx:
        effects, events = [], []
        fx.add_tool('lookup', lambda args: effects.append(1) or {})
        fx.queue_calls([{'id': 'private-native', 'name': 'lookup', 'arguments': '{}'}])
        def callback(event):
            events.append(event)
            if event['event'] == 'tool_completed':
                raise RuntimeError('private callback failure')
        result = fx.runtime.turn(fx.agent, message='Find', policy=fx.policy, on_event=callback)
        assert effects == [1] and result.completed_turn is None
        assert result.evidence.to_dict()['toolOutcomes'][0]['status'] == 'completed'
        assert result.result.metadata['failure_reason'] == 'observationFailure'
        assert 'private-native' not in json.dumps(events)


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_oversized_provisional_delta_fails_with_bounded_evidence(turn_fixture, provider):
    with turn_fixture(provider) as fx:
        fx.queue_stream_text('x' * 2000)
        events = []
        result = fx.runtime.turn(fx.agent, message='Hi', policy=fx.policy,
                                 limits=TurnLimits(max_event_bytes=512), on_event=events.append)
        assert result.completed_turn is None and result.result.metadata['failure_reason'] == 'outputLimit'
        assert all(len(json.dumps(e).encode()) <= 512 for e in events)
        assert result.evidence.to_dict()['usageComplete'] is False


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_ordinary_observer_errors_remain_best_effort(turn_fixture, provider):
    with turn_fixture(provider) as fx:
        fx.queue_text('done')
        def observer(record):
            raise RuntimeError('private observer failure')
        result = fx.runtime.turn(fx.agent, message='Hi', policy=fx.policy, observer=observer)
        assert result.result.exit_code == 0 and result.completed_turn is not None


def test_duplicate_envelope_failure_keeps_attempt_usage_unknown(turn_fixture):
    with turn_fixture('openai-compatible') as fx:
        fx.responses.append((200, 'application/json', b'{"choices":[],"choices":[]}'))
        result = fx.runtime.turn(fx.agent, message='Hi', policy=fx.policy)
        assert result.evidence.to_dict()['providerAttempts'] == 1
        assert result.evidence.to_dict()['usageComplete'] is False


def test_replay_observation_survives_cancellation_from_journal_claim(turn_fixture, tmp_path):
    import hashlib
    cancelled = []
    class Journal(SqliteJournal):
        def claim(self, *args):
            claim = super().claim(*args)
            if claim.state == 'replay':
                cancelled.append(True)
            return claim
    with turn_fixture('openai-compatible') as fx:
        fx.add_tool('lookup', lambda args, context: pytest.fail('replay invoked handler'),
                    operation_key=lambda args, context: 'effect')
        journal = Journal(tmp_path / 'journal.sqlite')
        signature = hashlib.sha256(b'{"arguments":{},"tool_name":"lookup","tool_version":"v1"}').hexdigest()
        claim = journal.claim('fixture-ops', 'effect', signature)
        journal.commit('fixture-ops', 'effect', claim.claim_token, {'status': 'ok', 'result': {}})
        fx.queue_calls([{'id': 'a', 'name': 'lookup', 'arguments': '{}'}])
        result = fx.runtime.turn(fx.agent, message='Find', policy=fx.policy, tool_journal=journal,
            operation_context=InvocationScope('attempt', operation_namespace='fixture-ops'),
            cancelled=lambda: bool(cancelled))
        assert result.result.metadata['failure_reason'] == 'cancelled'
        assert result.evidence.to_dict()['toolOutcomes'][0]['status'] == 'replayed'


def test_invalid_journal_claim_retains_uncertain_reference(turn_fixture):
    from prosaic_runtime import ToolClaim
    class Journal:
        identity = 'fixture-journal'
        contract_version = 'tool-journal-v1'
        def claim(self, namespace, key, signature):
            return ToolClaim('uncertain', '0' * 64)
        def commit(self, *args):
            pytest.fail('invalid claim committed')
    with turn_fixture('openai-compatible') as fx:
        fx.add_tool('lookup', lambda args, context: pytest.fail('invalid claim dispatched'),
                    operation_key=lambda args, context: 'effect')
        fx.queue_calls([{'id': 'a', 'name': 'lookup', 'arguments': '{}'}])
        result = fx.runtime.turn(fx.agent, message='Find', policy=fx.policy, tool_journal=Journal(),
            operation_context=InvocationScope('attempt', operation_namespace='fixture-ops'))
        assert result.result.metadata['failure_reason'] == 'toolEffectUncertain'
        assert result.evidence.to_dict()['toolOutcomes'][0]['status'] == 'uncertain'


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_native_response_json_work_bounds_apply_before_success(turn_fixture, provider):
    with turn_fixture(provider) as fx:
        fx.queue_text('done')
        status, content_type, body = fx.responses.pop()
        value = json.loads(body)
        value['extension'] = [0] * 600
        fx.responses.append((status, content_type, json.dumps(value).encode()))
        result = fx.runtime.turn(fx.agent, message='Hi', policy=fx.policy,
                                 limits=TurnLimits(max_json_nodes=512))
        assert result.completed_turn is None
        assert result.result.metadata['failure_reason'] == 'invalidToolRequest'


def test_conflicting_stream_usage_is_unknown_without_a_token_cap(turn_fixture):
    with turn_fixture('openai-compatible') as fx:
        fx.queue_stream_text('done')
        status, content_type, body = fx.responses.pop()
        conflict = {'choices': [], 'usage': {'prompt_tokens': 3, 'completion_tokens': 3, 'total_tokens': 6}}
        body = body.replace(b'data: [DONE]', b'data: ' + json.dumps(conflict).encode() + b'\n\ndata: [DONE]')
        fx.responses.append((status, content_type, body))
        result = fx.runtime.turn(fx.agent, message='Hi', policy=fx.policy)
        assert result.result.exit_code == 0 and result.result.token_usage is None
        assert result.evidence.to_dict()['usageComplete'] is False
        assert result.evidence.to_dict()['knownUsage'] == {}


def test_callbacks_cannot_switch_admitted_model_or_registered_implementation(turn_fixture):
    with turn_fixture('openai-compatible') as fx:
        effects = []
        fx.add_tool('lookup', lambda args: effects.append('approved') or {})
        rt = fx.runtime
        fx.queue_calls([{'id': 'a', 'name': 'lookup', 'arguments': '{}'}])
        fx.queue_text('done')
        replacement = CustomTool('lookup', 'Changed implementation',
            {'type': 'object', 'additionalProperties': False}, lambda args: effects.append('changed') or {}, 'v2')
        def observer(event):
            if event['event'] == 'invocation_started':
                rt.config.profiles['local'] = replace(rt.config.profiles['local'], model='changed-model')
        def sink(event):
            if event['event'] == 'started':
                rt._custom_tools['lookup'] = replacement
        result = rt.turn(fx.agent, message='Find', policy=fx.policy, observer=observer, on_event=sink)
        assert result.result.exit_code == 0 and effects == ['approved']
        assert [request['model'] for request in fx.requests] == ['fixture-model', 'fixture-model']


def test_private_uncertainty_reference_identifies_original_journal_address(turn_fixture, tmp_path):
    class Journal(SqliteJournal):
        def commit(self, *args):
            super().commit(*args)
            raise RuntimeError('lost acknowledgement')
    with turn_fixture('openai-compatible') as fx:
        fx.add_tool('lookup', lambda args, context: {}, operation_key=lambda args, context: 'effect')
        journal = Journal(tmp_path / 'journal.sqlite')
        fx.queue_calls([{'id': 'a', 'name': 'lookup', 'arguments': '{}'}])
        result = fx.runtime.turn(fx.agent, message='Find', policy=fx.policy, tool_journal=journal,
            operation_context=InvocationScope('attempt', operation_namespace='fixture-ops'))
        reference = result.evidence.to_dict()['toolOutcomes'][0]
        assert reference['status'] == 'uncertain' and reference['callIndex'] == 0
        assert (reference['operationNamespace'], reference['operationKey']) == ('fixture-ops', 'effect')
        assert reference['signature'] == journal.signature('fixture-ops', 'effect')
