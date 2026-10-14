"""Count final provider envelopes before effects, preserving prior tool evidence."""
from dataclasses import replace
import json
import time
import pytest
import prosaic_runtime as api
from support.context_counter import fixture_context
from support.conversation_server import turn_fixture


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
@pytest.mark.parametrize('streaming', [False, True])
def test_counted_request_matches_each_actual_native_wire(turn_fixture, provider, streaming):
    seen = []
    policy, counters = fixture_context(provider, count=lambda request: seen.append(request) or 10)
    with turn_fixture(provider) as fx:
        fx.streaming = streaming
        fx.add_tool('lookup', lambda args: {'value': 'found'})
        fx.queue_calls([{'id': 'call_0', 'name': 'lookup', 'arguments': '{}'}])
        fx.queue_text('done')
        rt = fx.runtime
        prepared = rt.prepare_turn(fx.agent, message='lookup', policy=fx.policy,
            context_policy=policy, token_counters=counters)
        result = rt.turn(prepared, token_counters=counters)
        assert result.result.exit_code == 0
        assert len(seen) == 3  # initial admission, readmission, one continuation
        assert seen[1:] == fx.requests
        assert result.context_report.candidate_checks == 3
        assert prepared.context_report.candidate_checks == 1
        assert result.context_work.readmission_pending is False


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
@pytest.mark.parametrize('failure', ['overflow', 'counter'])
def test_context_failure_after_tool_keeps_effect_and_real_usage(turn_fixture, provider, failure):
    effects = []
    def count(request):
        if 'large-tool-result' in json.dumps(request):
            if failure == 'counter':
                raise RuntimeError('private-counter-sentinel')
            return 1000
        return 10
    policy, counters = fixture_context(provider, capacity=200, reserve=20, count=count)
    with turn_fixture(provider) as fx:
        fx.add_tool('lookup', lambda args: effects.append('completed') or {'value': 'large-tool-result'})
        fx.queue_calls([{'id': 'call_0', 'name': 'lookup', 'arguments': '{}'}])
        result = fx.runtime.turn(fx.agent, message='lookup', policy=fx.policy,
            context_policy=policy, token_counters=counters)
        assert len(fx.requests) == 1
        assert effects == ['completed']
        assert result.result.exit_code >= 0
        assert result.completed_turn is None
        evidence = result.evidence.to_dict()
        assert evidence['failureCode'] == ('contextLimit' if failure == 'overflow' else 'bindingMismatch')
        assert evidence['providerAttempts'] == evidence['toolAttempts'] == 1
        assert evidence['knownUsage']['totalTokens'] == 5
        assert evidence['toolOutcomes'][0]['status'] == 'completed'
        assert 'private-counter-sentinel' not in json.dumps(evidence)
        assert 'private-counter-sentinel' not in result.result.stderr


def test_readmission_counter_checkpoint_precedes_dispatch_marker(turn_fixture):
    events = []
    policy, counters = fixture_context(count=lambda request: events.append('count') or 10)
    with turn_fixture('openai-compatible') as fx:
        fx.queue_text('done')
        rt = fx.runtime
        prepared = rt.prepare_turn(fx.agent, message='x', policy=fx.policy,
            context_policy=policy, token_counters=counters)
        events.clear()
        result = rt.turn(prepared, token_counters=counters,
            context_checkpoint=lambda state: events.append(('checkpoint', state.to_dict())),
            on_admitted=lambda: events.append('marker'))
        assert result.result.exit_code == 0
        index = events.index('count')
        assert events[index - 1][1]['reservedActiveMicros'] > 0
        assert events[index + 1][1]['reservedActiveMicros'] == 0
        assert events.index('marker') > index + 1
        assert len(fx.requests) == 1


def test_unknown_checkpoint_acknowledgement_stops_before_counter_or_provider(turn_fixture):
    calls = []
    policy, counters = fixture_context(count=lambda request: calls.append(1) or 10)
    with turn_fixture('openai-compatible') as fx:
        rt = fx.runtime
        prepared = rt.prepare_turn(fx.agent, message='x', policy=fx.policy,
            context_policy=policy, token_counters=counters)
        def fail(state):
            raise OSError('unknown storage acknowledgement')
        with pytest.raises(OSError):
            rt.turn(prepared, token_counters=counters, context_checkpoint=fail)
        assert calls == [1]  # only initial admission
        assert fx.requests == []


def test_missing_or_changed_counter_cannot_readmit_prepared_turn(turn_fixture):
    policy, counters = fixture_context()
    with turn_fixture('openai-compatible') as fx:
        rt = fx.runtime
        prepared = rt.prepare_turn(fx.agent, message='x', policy=fx.policy,
            context_policy=policy, token_counters=counters)
        with pytest.raises(api.TurnAdmissionError):
            rt.turn(prepared)
        entry = counters.resolve(policy.counter_identity)
        changed = api.TokenCounterRegistry({policy.counter_identity: api.TokenCounterRegistration(
            replace(entry.descriptor, tokenizer_assets_digest='2'*64), entry.counter)})
        with pytest.raises(api.TurnAdmissionError) as caught:
            rt.turn(prepared, token_counters=changed)
        assert caught.value.code == 'bindingMismatch'
        assert fx.requests == []


def test_provider_wait_is_excluded_from_assembly_active_time():
    from prosaic_runtime.context_work import ContextWorkBudget
    policy, _ = fixture_context(timeout_ms=1)
    now = [0]
    budget = ContextWorkBudget(policy, clock=lambda: now[0])
    budget.construct(lambda: None)
    now[0] = 10_000_000_000  # provider waiting outside assembly scopes
    assert budget.check_candidate(10, lambda: 1) == 1
    assert budget.state.active_assembly_micros == 0


def test_enclosing_deadline_keeps_timeout_classification():
    from prosaic_runtime.context_work import ContextWorkBudget
    from prosaic_runtime.tools import ToolDeadlineExceeded
    policy, _ = fixture_context()
    with pytest.raises(ToolDeadlineExceeded):
        ContextWorkBudget(policy, clock=lambda: 2_000_000_000, deadline=1)


def test_interrupted_active_reservation_cannot_be_reset(turn_fixture):
    policy, counters = fixture_context()
    with turn_fixture('openai-compatible') as fx:
        rt = fx.runtime
        prepared = rt.prepare_turn(fx.agent, message='x', policy=fx.policy,
            context_policy=policy, token_counters=counters)
        exhausted = api.ContextWorkState.from_dict({**prepared.context_work.to_dict(),
            'reservedActiveMicros': policy.assembly_timeout_ms * 1000})
        with pytest.raises(api.TurnAdmissionError) as caught:
            rt.turn(prepared, token_counters=counters, context_work=exhausted)
        assert caught.value.code == 'contextLimit'
        assert fx.requests == []


def test_prepared_work_override_cannot_reset_charged_active_time(turn_fixture):
    policy, counters = fixture_context()
    with turn_fixture('openai-compatible') as fx:
        rt = fx.runtime
        prepared = rt.prepare_turn(fx.agent, message='x', policy=fx.policy,
            context_policy=policy, token_counters=counters)
        assert prepared.context_work.active_assembly_micros > 0
        reset = api.ContextWorkState.from_dict({**prepared.context_work.to_dict(),
            'activeAssemblyMicros': 0})
        with pytest.raises(api.TurnAdmissionError) as caught:
            rt.turn(prepared, token_counters=counters, context_work=reset)
        assert caught.value.code == 'bindingMismatch'
        assert fx.requests == []


@pytest.mark.parametrize('opted_in', [False, True])
def test_predispatch_cancellation_keeps_existing_failed_result(turn_fixture, opted_in):
    with turn_fixture('openai-compatible') as fx:
        kwargs = {}
        if opted_in:
            policy, counters = fixture_context()
            kwargs.update(context_policy=policy, token_counters=counters)
        result = fx.runtime.turn(fx.agent, message='x', policy=fx.policy,
            cancelled=lambda: True, **kwargs)
        assert result.result.exit_code >= 0
        assert result.evidence.to_dict()['failureCode'] == 'cancelled'
        assert fx.requests == []


def test_slow_readmission_keeps_timeout_result_without_dispatch(turn_fixture):
    calls = []
    def count(request):
        calls.append(1)
        if len(calls) == 2:
            time.sleep(.03)
        return 10
    policy, counters = fixture_context(count=count)
    with turn_fixture('openai-compatible') as fx:
        result = fx.runtime.turn(fx.agent, message='x', policy=replace(fx.policy, timeout_s=.01),
            context_policy=policy, token_counters=counters)
        assert result.result.timed_out is True
        assert result.evidence.to_dict()['failureCode'] == 'timeout'
        assert fx.requests == []


def test_cancel_before_context_dispatch_keeps_frozen_accounting_owner(turn_fixture):
    from prosaic_runtime.accounting import MemoryRecorder
    class Recorder(MemoryRecorder):
        def prepare(self, intent):
            raise AssertionError('cancelled preflight must not allocate accounting intent')
    policy, counters = fixture_context()
    with turn_fixture('openai-compatible') as fx:
        result = fx.runtime.turn(fx.agent, message='x', policy=fx.policy,
            context_policy=policy, token_counters=counters, cancelled=lambda: True,
            accounting=Recorder(provider_id='openai-compatible'), context=api.ExecutionContext(
                application_id='app', tenant_id='owner', billing_account_id='payer'))
        evidence = result.evidence.to_dict()
        assert evidence['failureCode'] == 'cancelled'
        assert evidence['providerAttempts'] == 0 and fx.requests == []
        assert evidence['accounting']['context']['tenant_id'] == 'owner'
        assert evidence['accounting']['provider_call_ids'] == []


@pytest.mark.parametrize('failure', ['overflow', 'unknown', 'journal'])
def test_journal_status_and_attribution_survive_context_guard(turn_fixture, tmp_path, failure):
    from support.tool_journal import SqliteJournal
    from prosaic_runtime.accounting import MemoryRecorder
    effects = []
    class Journal(SqliteJournal):
        def commit(self, *args):
            super().commit(*args)
            if failure == 'journal':
                raise OSError('unknown commit acknowledgement')
    journal = Journal(tmp_path / 'effects.sqlite')
    if failure == 'unknown':
        import hashlib
        signature = hashlib.sha256(b'{"arguments":{},"tool_name":"lookup","tool_version":"v1"}').hexdigest()
        journal.claim('fixture-ops', 'effect', signature)
    policy, counters = fixture_context(capacity=200, reserve=20,
        count=lambda request: 1000 if 'large-tool-result' in json.dumps(request) else 10)
    with turn_fixture('openai-compatible') as fx:
        fx.add_tool('lookup', lambda args, context: effects.append(1) or {'value': 'large-tool-result'},
            operation_key=lambda args, context: 'effect')
        fx.queue_calls([{'id': 'a', 'name': 'lookup', 'arguments': '{}'}])
        result = fx.runtime.turn(fx.agent, message='x', policy=fx.policy,
            context_policy=policy, token_counters=counters, tool_journal=journal,
            operation_context=api.InvocationScope('attempt', operation_namespace='fixture-ops'),
            accounting=MemoryRecorder(provider_id='openai-compatible'), context=api.ExecutionContext(
                application_id='app', tenant_id='owner', billing_account_id='payer',
                request_id='request', invocation_id='attempt'))
        evidence = result.evidence.to_dict()
        assert len(fx.requests) == 1
        assert effects == ([] if failure == 'unknown' else [1])
        assert evidence['failureCode'] == {'overflow': 'contextLimit',
            'unknown': 'toolEffectUncertain', 'journal': 'toolJournalFailure'}[failure]
        outcome = evidence['toolOutcomes'][0]
        assert outcome['status'] == ('completed' if failure == 'overflow' else 'uncertain')
        assert outcome['operationNamespace'] == 'fixture-ops'
        assert outcome['operationKey'] == 'effect'
        assert len(outcome['operationId']) == 64
        assert evidence['knownUsage']['totalTokens'] == 5
        assert evidence['accounting']['context']['tenant_id'] == 'owner'
