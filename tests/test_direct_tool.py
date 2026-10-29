"""Public host-admitted model-free execution; no provider fixture is needed."""
import json
import time
from dataclasses import replace
from threading import Event, Thread

import pytest
from prosaic_runtime import (CustomTool, ExecutionBudget, InvocationScope, ProsaicRuntime,
                             RunPolicy, RuntimeConfig, EndpointConfig)
from prosaic_runtime.accounting import ExecutionContext, MemoryRecorder, ResolvedContext
from prosaic_runtime.lifecycle import Draining
from prosaic_runtime.tool_effects import ToolClaim, ToolFailure

SCHEMA = {'type': 'object', 'properties': {'value': {'type': 'integer'}},
          'required': ['value'], 'additionalProperties': False}
POLICY = RunPolicy(allowed_tools=frozenset({'lookup'}))


def runtime(handler=lambda args: args, **tool_options):
    tool = CustomTool('lookup', 'Lookup', SCHEMA, handler, 'v1', **tool_options)
    config = RuntimeConfig({'small': EndpointConfig('http://localhost:9/v1', 'small')},
                           {'fast': 'small'}, 'small', frozenset({'lookup'}))
    return ProsaicRuntime(config, custom_tools={'lookup': tool})


def test_actual_value_zero_provider_calls_and_safe_events(monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *a: pytest.fail('provider transport'))
    recorder, events, records, seen = MemoryRecorder(), [], [], []
    rt = runtime(lambda a: seen.append(a) or {'answer': a['value'] + 1})
    result = rt.run_tool('lookup', {'value': 2}, policy=POLICY, on_event=events.append,
                         observer=records.append, accounting=recorder)
    assert 'direct_tool_v1' in rt.capabilities
    assert result.exit_code == 0 and json.loads(result.stdout) == {'answer': 3}
    assert result.token_usage == 0 and result.cost_usd == 0
    assert result.metadata['direct_tool_v1'] == {'model_free': True, 'provider_requests': 0}
    assert result.metadata['invocation_budgets_v1']['tool_calls'] == 1
    assert seen == [{'value': 2}] and not recorder.intents and not recorder.observations
    assert [e['event'] for e in events] == ['tool_started', 'tool_completed']
    assert all(e['version'] == 1 for e in events)
    assert [e['event'] for e in records] == ['invocation_started', 'tool_started', 'tool_completed', 'invocation_completed']
    assert all('answer' not in json.dumps(e) and 'value' not in json.dumps(e) for e in events + records)


@pytest.mark.parametrize('name,args,policy,reason', [
    ('unknown', {'value': 1}, POLICY, 'not_registered'),
    ('read_file', {}, POLICY, 'unsupported_direct_tool'),
    ('lookup', {'value': 1}, RunPolicy(), 'not_granted'),
    ('lookup', {'value': 'secret'}, POLICY, 'invalid_arguments'),
    ('lookup', {'value': float('nan')}, POLICY, 'invalid_arguments'),
    ('lookup', '{"value":1}', POLICY, 'invalid_arguments'),
    ('lookup', {'value': 1}, replace(POLICY, max_tool_calls=0), 'tool_call_limit'),
])
def test_admission_before_effects(name, args, policy, reason):
    seen = []
    result = runtime(lambda a: seen.append(a)).run_tool(name, args, policy=policy)
    assert result.exit_code != 0 and result.metadata['failure_reason'] == reason
    assert result.token_usage == 0 and seen == []
    assert 'secret' not in result.stderr


def test_runtime_grant_and_authorizer_required():
    rt = runtime(authorize=lambda a: False)
    assert rt.run_tool('lookup', {'value': 1}, policy=POLICY).metadata['failure_reason'] == 'authorization_denied'
    rt.config = replace(rt.config, allowed_tools=frozenset())
    assert rt.run_tool('lookup', {'value': 1}, policy=POLICY).metadata['failure_reason'] == 'not_granted'


@pytest.mark.parametrize('options,handler,reason', [
    ({'max_argument_bytes': 2}, lambda a: 1, 'invalid_arguments'),
    ({'max_result_bytes': 20}, lambda a: 'x' * 30, 'result_limit'),
    ({}, lambda a: object(), 'invalid_result'),
    ({}, lambda a: ToolFailure('unavailable'), 'unavailable'),
])
def test_bounded_outputs(options, handler, reason):
    result = runtime(handler, **options).run_tool('lookup', {'value': 1}, policy=POLICY)
    assert result.exit_code == 1 and result.metadata['failure_reason'] == reason


def test_context_is_resolved_trusted_host_identity():
    contexts = []
    rt = runtime(lambda a, ctx: contexts.append(ctx) or a, with_context=True)
    context = ExecutionContext(application_id='app', tenant_id='trusted', billing_account_id='payer')
    scope = InvocationScope('op', run_id='run', step_id='step')
    result = rt.run_tool('lookup', {'value': 3}, policy=POLICY, context=context, operation_context=scope)
    assert result.exit_code == 0
    ctx = contexts[0]
    assert ctx.scope == scope and ctx.call_index == 0 and ctx.tool_version == 'v1'
    assert type(ctx.accounting_context) is ResolvedContext
    assert ctx.accounting_context.tenant_id == 'trusted'
    assert result.metadata['accounting_v1']['context'] == ctx.accounting_context.to_dict()
    assert result.metadata['accounting_v1']['provider_call_ids'] == []


def test_cancel_deadline_and_shared_budgets():
    seen = []
    rt = runtime(lambda a: seen.append(a) or time.sleep(.02))
    assert rt.run_tool('lookup', {'value': 1}, policy=POLICY, cancelled=lambda: True).metadata['failure_reason'] == 'cancelled'
    assert not seen
    result = rt.run_tool('lookup', {'value': 1}, policy=replace(POLICY, timeout_s=.001))
    assert result.timed_out and result.metadata['failure_reason'] == 'invocation_timeout'
    shared = ExecutionBudget(timeout_s=10, max_provider_requests=0, max_tool_calls=1)
    with shared.activate():
        assert rt.run_tool('lookup', {'value': 1}, policy=POLICY).exit_code == 0
        assert rt.run_tool('lookup', {'value': 1}, policy=POLICY).metadata['failure_reason'] == 'tool_call_limit'
    assert shared.tool_calls == 1 and shared.provider_requests == shared.reported_tokens == 0


class Journal:
    identity = 'memory-test'
    contract_version = 'tool-journal-v1'
    def __init__(self): self.entries = {}
    def claim(self, namespace, key, signature):
        identity = namespace, key
        if identity in self.entries:
            saved, outcome = self.entries[identity]
            return ToolClaim('replay' if outcome else 'uncertain', saved, outcome=outcome)
        self.entries[identity] = signature, None
        return ToolClaim('new', signature, 'claim')
    def commit(self, namespace, key, token, outcome):
        self.entries[namespace, key] = self.entries[namespace, key][0], outcome


def test_journal_replay_and_uncertainty_fence():
    seen, journal = [], Journal()
    scope = InvocationScope('op', operation_namespace='namespace')
    rt = runtime(lambda a, c: seen.append(a) or a, with_context=True, operation_key=lambda a, c: 'key')
    kwargs = dict(policy=POLICY, operation_context=scope, tool_journal=journal)
    first = rt.run_tool('lookup', {'value': 1}, **kwargs)
    second = rt.run_tool('lookup', {'value': 1}, **kwargs)
    assert first.stdout == second.stdout and len(seen) == 1
    assert second.metadata['tool_outcome']['replayed'] is True
    journal.entries['namespace', 'key'] = journal.entries['namespace', 'key'][0], None
    result = rt.run_tool('lookup', {'value': 1}, **kwargs)
    assert result.metadata['failure_reason'] == 'tool_effect_uncertain' and len(seen) == 1


def test_lifecycle_drain_waits_for_callback():
    entered, release = Event(), Event()
    rt = runtime(lambda a: entered.set() or release.wait(2) or a)
    worker = Thread(target=lambda: rt.run_tool('lookup', {'value': 1}, policy=POLICY))
    worker.start()
    assert entered.wait(2)
    assert not rt.lifecycle.wait(.001)
    with pytest.raises(Draining): rt.run_tool('lookup', {'value': 1}, policy=POLICY)
    release.set()
    worker.join(2)
    assert not worker.is_alive() and rt.lifecycle.wait(.1)


def test_exception_fenced_and_terminal_events_delivered():
    def handler(args): raise RuntimeError('PRIVATE callback secret')
    events, records = [], []
    result = runtime(handler).run_tool('lookup', {'value': 1}, policy=POLICY,
                                      on_event=events.append, observer=records.append)
    assert result.metadata['failure_reason'] == 'handler_error'
    assert result.metadata['tool_outcome']['effect'] == 'unknown'
    assert events[-1]['event'] == 'tool_completed' and events[-1]['status'] == 'error'
    assert 'PRIVATE' not in result.stderr + json.dumps(events + records)


def test_uncertain_journal_still_completes_events():
    journal, events = Journal(), []
    def handler(a, c): raise RuntimeError('PRIVATE')
    rt = runtime(handler, with_context=True, operation_key=lambda a, c: 'key')
    kwargs = dict(policy=POLICY, operation_context=InvocationScope('op', operation_namespace='ns'), tool_journal=journal)
    first = rt.run_tool('lookup', {'value': 1}, on_event=events.append, **kwargs)
    second = rt.run_tool('lookup', {'value': 1}, **kwargs)
    assert first.metadata['failure_reason'] == second.metadata['failure_reason'] == 'tool_effect_uncertain'
    assert [e['event'] for e in events] == ['tool_started', 'tool_completed']


def test_cancellation_after_callback_keeps_completion_event():
    state, events = [], []
    result = runtime(lambda a: state.append(1) or a).run_tool('lookup', {'value': 1}, policy=POLICY,
        cancelled=lambda: bool(state), on_event=events.append)
    assert result.metadata['failure_reason'] == 'cancelled'
    assert [e['event'] for e in events] == ['tool_started', 'tool_completed']


def test_context_snapshot_precedes_observer():
    defaults = ExecutionContext(application_id='app', tenant_id='trusted', billing_account_id='payer')
    rt = runtime(lambda a, ctx: {'tenant': ctx.accounting_context.tenant_id}, with_context=True)
    rt.context_defaults = defaults
    def observer(record):
        rt.context_defaults = ExecutionContext(application_id='app', tenant_id='changed', billing_account_id='wrong')
    result = rt.run_tool('lookup', {'value': 1}, policy=POLICY, observer=observer)
    assert json.loads(result.stdout)['tenant'] == 'trusted'


def test_cli_not_admitted_and_missing_journal_rejected():
    seen = []
    rt = runtime(lambda a, c: seen.append(a), with_context=True, operation_key=lambda a, c: 'key')
    result = rt.run_tool('lookup', {'value': 1}, policy=POLICY)
    assert result.metadata['failure_reason'] == 'admission_failure' and not seen
    rt._cli_tools['lookup'] = object()
    result = rt.run_tool('lookup', {'value': 1}, policy=POLICY)
    assert result.metadata['failure_reason'] == 'unsupported_direct_tool' and not seen


def test_event_hook_error_stops_before_tool_and_observer_errors_are_best_effort():
    seen = []
    rt = runtime(lambda a: seen.append(a) or a)
    def bad(event): raise RuntimeError('PRIVATE')
    result = rt.run_tool('lookup', {'value': 1}, policy=POLICY, on_event=bad)
    assert result.metadata['failure_reason'] == 'critical_hook_error' and not seen
    assert 'PRIVATE' not in result.stderr
    assert rt.run_tool('lookup', {'value': 1}, policy=POLICY, observer=bad).exit_code == 0


@pytest.mark.parametrize('value', [None, True, 0, 'actual value', ['a', {'b': 1}]])
def test_actual_json_callback_value_is_not_rewritten(value):
    result = runtime(lambda a: value).run_tool('lookup', {'value': 1},
        policy=replace(POLICY, max_provider_requests=0))
    assert result.exit_code == 0 and json.loads(result.stdout) == value
    assert result.metadata['invocation_budgets_v1']['provider_requests'] == 0


def test_shared_cancel_and_authorizer_deadline_stop_before_handler():
    seen = []
    rt = runtime(lambda a: seen.append(a))
    shared = ExecutionBudget(timeout_s=10, max_provider_requests=0, max_tool_calls=1, cancelled=lambda: True)
    with shared.activate():
        result = rt.run_tool('lookup', {'value': 1}, policy=POLICY)
    assert result.metadata['failure_reason'] == 'cancelled' and not seen
    rt = runtime(lambda a: seen.append(a), authorize=lambda a: time.sleep(.02) or True)
    result = rt.run_tool('lookup', {'value': 1}, policy=replace(POLICY, timeout_s=.001))
    assert result.timed_out and not seen


def test_journal_commit_precedes_cancellation_and_replay_uses_receipt():
    state, journal = [], Journal()
    rt = runtime(lambda a, ctx: state.append(1) or a, with_context=True, operation_key=lambda a, c: 'key')
    kwargs = dict(policy=POLICY, operation_context=InvocationScope('op', operation_namespace='ns'), tool_journal=journal)
    cancelled = rt.run_tool('lookup', {'value': 1}, cancelled=lambda: bool(state), **kwargs)
    replayed = rt.run_tool('lookup', {'value': 1}, **kwargs)
    assert cancelled.metadata['failure_reason'] == 'cancelled'
    assert replayed.exit_code == 0 and replayed.metadata['tool_outcome']['replayed']
    assert state == [1]


def test_literal_arguments_owned_before_observation():
    arguments = {'value': 1}
    def observer(event): arguments['value'] = 99
    result = runtime().run_tool('lookup', arguments, policy=POLICY, observer=observer)
    assert json.loads(result.stdout) == {'value': 1}
