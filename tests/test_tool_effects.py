"""Consequential dispatch must never mistake an uncertain effect for a retry."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import hashlib
import json
import time
from threading import Barrier

import pytest
import prosaic_runtime as public
from prosaic_runtime import CustomTool, InvocationScope, ProsaicRuntime, RunPolicy, Result
from prosaic_runtime import StructuredToolLoop, StructuredToolError, ToolRequest, ToolClaim
from prosaic_runtime import ToolExecutionContext, tool_journal_descriptor
from prosaic_runtime.events import Cancelled
from prosaic_runtime.accounting import ExecutionContext, MemoryRecorder
from prosaic_runtime.tools import ToolDeadlineExceeded
from support.tool_journal import SqliteJournal
from test_custom_tool_transport import setup, call
from test_runtime import server, artifact, completion


SCHEMA = {'type': 'object', 'properties': {'id': {'type': 'string'}},
          'required': ['id'], 'additionalProperties': False}
ARGS = {'id': 'é'}


def definition(handler=lambda args, ctx: {'value': args['id']}, **kwargs):
    return CustomTool('lookup', 'Lookup', SCHEMA, handler, 'v2', with_context=True,
                      operation_key=kwargs.pop('operation_key', lambda args, ctx: 'business-key'), **kwargs)


def session(tool, journal=None, namespace='tenant', **kwargs):
    return StructuredToolLoop({'lookup': tool}, allowed_tools={'lookup'}, arguments={},
        deadline=time.monotonic() + 10, tool_journal=journal,
        operation_context=InvocationScope('attempt', run_id='run', step_id='step',
                                          operation_namespace=namespace), **kwargs)


def dispatch(tool, journal, namespace='tenant', **kwargs):
    return session(tool, journal, namespace, **kwargs).dispatch(ToolRequest('lookup', ARGS))


def test_journal_replay_does_not_execute_twice(server, tmp_path):
    url, requests, responses = server
    seen = []
    config, original = setup(url, seen)
    tool = CustomTool('lookup_catalog', 'Lookup', original.parameters,
        lambda args, ctx: seen.append(ctx.operation_key) or {'found': True}, 'v2',
        with_context=True, operation_key=lambda args, ctx: 'selection-1')
    journal = SqliteJournal(tmp_path / 'journal.sqlite')
    runtime = ProsaicRuntime(config, custom_tools={'lookup_catalog': tool})
    for attempt in ('first', 'second'):
        responses.extend([completion('', [call()]), completion('done')])
        result = runtime.run(artifact(['lookup_catalog']),
            policy=RunPolicy(allowed_tools=frozenset({'lookup_catalog'})), tool_journal=journal,
            operation_context=InvocationScope(attempt, operation_namespace='tenant-a'))
        assert result.exit_code == 0
    assert seen == ['selection-1'] and len(requests) == 4


def test_context_is_host_owned_and_each_callback_gets_independent_arguments(tmp_path):
    seen = []
    def authorize(args, ctx):
        seen.append(('authorize', dict(args), ctx))
        args['id'] = 'authorization mutation'
        return True
    def resolver(args, ctx):
        seen.append(('resolver', dict(args), ctx))
        args['id'] = 'resolver mutation'
        return 'business-key'
    def handler(args, ctx):
        seen.append(('handler', dict(args), ctx))
        args['id'] = 'handler mutation'
        return {'value': 'é'}
    tool = definition(handler, authorize=authorize, operation_key=resolver)
    journal = SqliteJournal(tmp_path / 'journal.sqlite')
    record, value = dispatch(tool, journal)
    assert record['arguments'] == ARGS and value == {'value': 'é'}
    assert [(entry[0], entry[1]) for entry in seen] == [
        ('authorize', ARGS), ('resolver', ARGS), ('handler', ARGS)]
    digest = hashlib.sha256(b'{"id":"\xc3\xa9"}').hexdigest()
    for _, _, ctx in seen:
        assert type(ctx) is ToolExecutionContext and ctx.arguments_sha256 == digest
        assert ctx.scope == InvocationScope('attempt', run_id='run', step_id='step', operation_namespace='tenant')
        assert ctx.tool_name == 'lookup' and ctx.tool_version == 'v2' and ctx.call_index == 0
        assert ctx.deadline > time.monotonic() and ctx.cancelled() is False
        with pytest.raises(FrozenInstanceError):
            ctx.tool_name = 'mutated'
    assert [ctx.operation_key for _, _, ctx in seen] == [None, None, 'business-key']
    expected = hashlib.sha256('{"arguments":{"id":"é"},"tool_name":"lookup","tool_version":"v2"}'.encode()).hexdigest()
    assert journal.signature('tenant', 'business-key') == expected


def test_contextual_unjournaled_and_ordinary_callbacks_keep_explicit_arity():
    seen = []
    tool = definition(lambda args, ctx: seen.append(ctx) or {}, operation_key=None,
                      authorize=lambda args, ctx: ctx.operation_key is None)
    assert dispatch(tool, None)[1] == {} and seen[0].operation_key is None
    ordinary = CustomTool('lookup', 'Lookup', SCHEMA, lambda args: {'value': args['id']}, 'v1',
                          authorize=lambda args: True)
    assert dispatch(ordinary, None)[1] == {'value': 'é'}
    assert set(ordinary.descriptor) == {'name', 'description', 'parameters', 'version',
        'max_argument_bytes', 'max_result_bytes', 'authorization_required'}
    assert tool.descriptor['with_context'] is True and 'journaled' not in tool.descriptor
    assert definition().descriptor['journaled'] is True
    assert ordinary.descriptor == {'name': 'lookup', 'description': 'Lookup', 'parameters': SCHEMA,
        'version': 'v1', 'max_argument_bytes': 16384, 'max_result_bytes': 65536,
        'authorization_required': True}


@pytest.mark.parametrize('kwargs', [{'with_context': 1}, {'operation_key': lambda args, ctx: 'k'},
                                    {'with_context': True, 'operation_key': 'key'}])
def test_context_optin_configuration_is_explicit(kwargs):
    with pytest.raises(ValueError):
        CustomTool('lookup', 'Lookup', SCHEMA, lambda args: {}, 'v1', **kwargs)


def test_journal_descriptor_is_pure_and_rejects_dynamic_metadata():
    seen = []
    class Journal:
        contract_version = 'tool-journal-v1'
        identity = 'synthetic-journal'
        def claim(self, *args): seen.append('claim')
        def commit(self, *args): seen.append('commit')
    assert tool_journal_descriptor(None) == {}
    assert tool_journal_descriptor(Journal()) == {'contract_version': 'tool-journal-v1', 'identity': 'synthetic-journal'}
    for attribute in ('identity', 'contract_version', 'claim', 'commit'):
        class Dynamic(Journal): pass
        setattr(Dynamic, attribute, property(lambda self: seen.append('property')))
        with pytest.raises(ValueError): tool_journal_descriptor(Dynamic())
    assert seen == []
    assert hasattr(public, 'ToolJournal') and 'tool_journal_v1' in ProsaicRuntime.capabilities
    assert 'tool_context_v1' in ProsaicRuntime.capabilities


@pytest.mark.parametrize('identity', ['', 'a' * 129, 'é' * 65, 'a\n', 'a\u200b', 7])
def test_invalid_plain_journal_identity_rejected(identity):
    class Journal:
        contract_version = 'tool-journal-v1'
        claim = staticmethod(lambda *args: None)
        commit = staticmethod(lambda *args: None)
    journal = Journal(); journal.identity = identity
    with pytest.raises(ValueError): tool_journal_descriptor(journal)


@pytest.mark.parametrize('missing', ['scope', 'namespace', 'journal'])
def test_journal_requirements_fail_before_http_and_callbacks(server, tmp_path, missing):
    url, requests, responses = server
    seen = []
    config, original = setup(url, seen)
    tool = CustomTool('lookup_catalog', 'Lookup', original.parameters,
        lambda args, ctx: seen.append('handler'), 'v2', with_context=True,
        operation_key=lambda args, ctx: seen.append('resolver') or 'key',
        authorize=lambda args, ctx: seen.append('authorize') or True)
    options = {'tool_journal': SqliteJournal(tmp_path / 'journal.sqlite'),
               'operation_context': InvocationScope('attempt', operation_namespace='tenant')}
    if missing == 'scope': options.pop('operation_context')
    if missing == 'namespace': options['operation_context'] = InvocationScope('attempt')
    if missing == 'journal': options.pop('tool_journal')
    with pytest.raises(ValueError):
        ProsaicRuntime(config, custom_tools={'lookup_catalog': tool}).run(artifact(['lookup_catalog']),
            policy=RunPolicy(allowed_tools=frozenset({'lookup_catalog'})), **options)
    assert seen == [] and requests == []


def test_separate_namespaces_execute_and_same_namespace_conflicts(tmp_path):
    seen = []
    journal = SqliteJournal(tmp_path / 'journal.sqlite')
    tool = definition(lambda args, ctx: seen.append(ctx.scope.operation_namespace) or {})
    dispatch(tool, journal, 'tenant-a'); dispatch(tool, journal, 'tenant-b')
    assert seen == ['tenant-a', 'tenant-b']
    changed = CustomTool('lookup', 'Lookup', SCHEMA, tool.handler, 'v3', with_context=True,
                         operation_key=tool.operation_key)
    with pytest.raises(StructuredToolError, match='tool_identity_conflict'): dispatch(changed, journal, 'tenant-a')
    with pytest.raises(StructuredToolError, match='tool_identity_conflict'):
        session(tool, journal, 'tenant-a').dispatch(ToolRequest('lookup', {'id': 'changed'}))
    assert seen == ['tenant-a', 'tenant-b']


@pytest.mark.parametrize('failure', ['nan', 'oversize', 'raise', 'cancel', 'deadline'])
def test_post_effect_failure_preserves_uncertainty_and_never_reexecutes(tmp_path, failure):
    seen = []
    def handler(args, ctx):
        seen.append('effect')
        if failure == 'raise': raise RuntimeError('private-secret')
        if failure == 'cancel': raise Cancelled('private-secret')
        if failure == 'deadline': raise ToolDeadlineExceeded('private-secret')
        return float('nan') if failure == 'nan' else 'x' * 100
    tool = definition(handler, max_result_bytes=64)
    journal = SqliteJournal(tmp_path / 'journal.sqlite')
    for _ in range(2):
        with pytest.raises(StructuredToolError, match='^tool_effect_uncertain$'): dispatch(tool, journal)
    assert seen == ['effect']
    assert journal.claim('tenant', 'business-key', journal.signature('tenant', 'business-key')).state == 'uncertain'


@pytest.mark.parametrize('stop', ['cancel', 'deadline'])
def test_boundary_after_handler_commits_before_stopping(tmp_path, stop, monkeypatch):
    seen = []
    clock = time.monotonic
    monkeypatch.setattr(time, 'monotonic', lambda: clock() + (100 if seen and stop == 'deadline' else 0))
    journal = SqliteJournal(tmp_path / 'journal.sqlite')
    tool = definition(lambda args, ctx: seen.append('effect') or {'value': 'done'})
    loop = session(tool, journal, cancelled=lambda: bool(seen) and stop == 'cancel')
    with pytest.raises(StructuredToolError, match='execution-cancelled' if stop == 'cancel' else 'execution-timeout'):
        loop.dispatch(ToolRequest('lookup', ARGS))
    assert journal.claim('tenant', 'business-key', journal.signature('tenant', 'business-key')).state == 'replay'


def test_lost_commit_ack_is_critical_but_durable_outcome_can_replay(tmp_path):
    class LostAck(SqliteJournal):
        def commit(self, *args):
            super().commit(*args)
            raise RuntimeError('private-secret')
    seen = []
    tool = definition(lambda args, ctx: seen.append('effect') or {'value': 'done'})
    journal = LostAck(tmp_path / 'journal.sqlite')
    with pytest.raises(StructuredToolError, match='^tool_journal_failed$'): dispatch(tool, journal)
    assert dispatch(tool, journal)[1] == {'value': 'done'}
    assert seen == ['effect']


@pytest.mark.parametrize('key', ['', 'é' * 129, 'a' * 257, 'a\n', 'a\u200b', None, 7])
def test_invalid_host_key_never_claims_or_dispatches(tmp_path, key):
    seen = []
    journal = SqliteJournal(tmp_path / 'journal.sqlite')
    tool = definition(lambda args, ctx: seen.append('effect'), operation_key=lambda args, ctx: key)
    with pytest.raises(StructuredToolError, match='tool_identity_conflict'): dispatch(tool, journal)
    assert seen == []


def test_authorization_denial_never_resolves_or_claims(tmp_path):
    seen = []
    tool = definition(lambda args, ctx: seen.append('handler'),
        operation_key=lambda args, ctx: seen.append('resolver') or 'key', authorize=lambda args, ctx: False)
    journal = SqliteJournal(tmp_path / 'journal.sqlite')
    with pytest.raises(StructuredToolError, match='tool-authorization_denied'): dispatch(tool, journal)
    assert seen == []
    with journal.connection() as connection:
        assert connection.execute('SELECT COUNT(*) FROM effects').fetchone() == (0,)


@pytest.mark.parametrize('shape', ['dict', 'bad_signature', 'mismatch', 'bad_state', 'missing_token',
    'invalid_token', 'new_outcome', 'replay_token', 'replay_error', 'replay_extra', 'replay_nan',
    'replay_oversize', 'uncertain_token', 'uncertain_outcome'])
def test_invalid_claim_never_dispatches(shape):
    seen = []
    class Journal:
        identity = 'synthetic'; contract_version = 'tool-journal-v1'
        def claim(self, namespace, key, signature):
            claims = {
                'dict': {'state': 'new', 'signature': signature, 'claim_token': 'token'},
                'bad_signature': ToolClaim('new', 'G' * 64, 'token'),
                'mismatch': ToolClaim('new', 'a' * 64, 'token'),
                'bad_state': ToolClaim('expired', signature),
                'missing_token': ToolClaim('new', signature),
                'invalid_token': ToolClaim('new', signature, 'a\n'),
                'new_outcome': ToolClaim('new', signature, 'token', {'status': 'ok', 'result': {}}),
                'replay_token': ToolClaim('replay', signature, 'token', {'status': 'ok', 'result': {}}),
                'replay_error': ToolClaim('replay', signature, outcome={'status': 'error', 'error': 'bad'}),
                'replay_extra': ToolClaim('replay', signature, outcome={'status': 'ok', 'result': {}, 'extra': 1}),
                'replay_nan': ToolClaim('replay', signature, outcome={'status': 'ok', 'result': float('nan')}),
                'replay_oversize': ToolClaim('replay', signature, outcome={'status': 'ok', 'result': 'x' * 100}),
                'uncertain_token': ToolClaim('uncertain', signature, 'token'),
                'uncertain_outcome': ToolClaim('uncertain', signature, outcome={'status': 'ok', 'result': {}}),
            }
            return claims[shape]
        def commit(self, *args): seen.append('commit')
    tool = definition(lambda args, ctx: seen.append('effect'), max_result_bytes=64)
    reason = 'tool_identity_conflict' if shape == 'mismatch' else 'tool_journal_failed'
    with pytest.raises(StructuredToolError, match='^' + reason + '$'): dispatch(tool, Journal())
    assert seen == []


def test_replay_outcome_is_copied_and_current_result_limit_is_enforced():
    outcome = {'status': 'ok', 'result': {'value': ['initial']}}
    class Journal:
        identity = 'synthetic'; contract_version = 'tool-journal-v1'
        def claim(self, namespace, key, signature): return ToolClaim('replay', signature, outcome=outcome)
        def commit(self, *args): raise AssertionError('must not commit replay')
    journal = Journal()
    dispatch(definition(), journal)[1]['value'].append('mutated')
    assert outcome['result']['value'] == ['initial']
    with pytest.raises(StructuredToolError, match='tool_journal_failed'):
        dispatch(definition(max_result_bytes=10), journal)


def test_concurrent_claims_and_stale_fence_cannot_overwrite(tmp_path):
    path = tmp_path / 'journal.sqlite'; SqliteJournal(path)
    signature = 'a' * 64
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: SqliteJournal(path).claim('tenant', 'key', signature), range(2)))
    assert sorted(claim.state for claim in claims) == ['new', 'uncertain']
    journal = SqliteJournal(path)
    winner = next(claim for claim in claims if claim.state == 'new')
    outcome = {'status': 'ok', 'result': {'value': 1}}
    with pytest.raises(ValueError): journal.commit('tenant', 'key', 'stale-token', outcome)
    journal.commit('tenant', 'key', winner.claim_token, outcome)
    with pytest.raises(ValueError): journal.commit('tenant', 'key', winner.claim_token, {'status': 'ok', 'result': 2})
    assert journal.claim('tenant', 'key', signature).outcome == outcome


def test_journal_failure_never_enters_structured_correction():
    class Journal:
        identity = 'synthetic'; contract_version = 'tool-journal-v1'
        def claim(self, *args): raise RuntimeError('private-secret')
        def commit(self, *args): raise AssertionError('must not commit')
    events = []
    driver = session(definition(), Journal(), on_event=events.append).turns(admit=lambda proposal: None)
    next(driver)
    with pytest.raises(StructuredToolError, match='^tool_journal_failed$'):
        driver.send(Result(0, '{"tool":{"name":"lookup","arguments":{"id":"é"}}}', ''))
    assert not any(event['type'] == 'correction' for event in events)


def test_runtime_effect_failure_retains_completed_usage_without_followup(server, tmp_path):
    url, requests, responses = server
    seen, observed = [], []
    config, original = setup(url, seen)
    tool = CustomTool('lookup_catalog', 'Lookup', original.parameters,
        lambda args, ctx: seen.append('effect') or float('nan'), 'v2', with_context=True,
        operation_key=lambda args, ctx: 'business-key')
    responses.extend([completion('', [call()]), completion('must never execute')])
    result = ProsaicRuntime(config, custom_tools={'lookup_catalog': tool}).run(artifact(['lookup_catalog']),
        policy=RunPolicy(allowed_tools=frozenset({'lookup_catalog'})),
        operation_context=InvocationScope('attempt', operation_namespace='tenant'),
        tool_journal=SqliteJournal(tmp_path / 'journal.sqlite'), observer=observed.append)
    assert result.exit_code == 1 and result.metadata['failure_reason'] == 'tool_effect_uncertain'
    assert result.token_usage == 7 and result.metadata['usage_scope'] == 'reported_completed_turns'
    assert seen == ['effect'] and len(requests) == 1
    assert observed[-1]['reason'] == 'tool_effect_uncertain'


def test_structured_observer_is_best_effort_and_context_call_index_increments():
    seen, events = [], []
    tool = definition(lambda args, ctx: seen.append(ctx.call_index) or {}, operation_key=None)
    driver = session(tool, None, observer=events.append, max_identical_calls=2)
    driver.dispatch(ToolRequest('lookup', ARGS)); driver.dispatch(ToolRequest('lookup', ARGS))
    assert seen == [0, 1]
    assert [event['event'] for event in events] == ['tool_started', 'tool_completed'] * 2
    broken = session(tool, None, observer=lambda event: (_ for _ in ()).throw(ValueError('private')))
    assert broken.dispatch(ToolRequest('lookup', ARGS))[1] == {}


def test_contextual_non_utf8_arguments_are_rejected_before_callbacks():
    seen = []
    tool = definition(lambda args, ctx: seen.append('effect'), operation_key=None,
                      authorize=lambda args, ctx: seen.append('authorize') or True)
    with pytest.raises(StructuredToolError, match='tool-invalid_arguments'):
        session(tool).dispatch(ToolRequest('lookup', {'id': '\ud800'}))
    assert seen == []


def test_concurrent_dispatch_executes_one_effect_and_blocks_uncertain_peer(tmp_path):
    barrier, effects = Barrier(2), []
    class ConcurrentJournal(SqliteJournal):
        def claim(self, *args):
            barrier.wait(timeout=5)
            claim = super().claim(*args)
            # Both claims complete before either worker can dispatch/commit.
            barrier.wait(timeout=5)
            return claim
    path = tmp_path / 'journal.sqlite'
    journals = [ConcurrentJournal(path), ConcurrentJournal(path)]
    tool = definition(lambda args, ctx: effects.append(ctx.operation_key) or {})
    def invoke(journal):
        try: return dispatch(tool, journal)[1]
        except StructuredToolError as error: return error.reason
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(invoke, journals))
    assert outcomes.count({}) == 1 and outcomes.count('tool_effect_uncertain') == 1
    assert effects == ['business-key']


def test_replay_still_requires_current_authorization(tmp_path):
    allowed, effects = [True], []
    tool = definition(lambda args, ctx: effects.append('effect') or {}, authorize=lambda args, ctx: allowed[0])
    journal = SqliteJournal(tmp_path / 'journal.sqlite')
    dispatch(tool, journal); allowed[0] = False
    with pytest.raises(StructuredToolError, match='tool-authorization_denied'): dispatch(tool, journal)
    assert effects == ['effect']


@pytest.mark.parametrize('stage', ['resolver', 'claim', 'commit'])
def test_host_failure_does_not_leak_and_commit_failure_stays_uncertain(tmp_path, stage):
    effects = []
    class FailedJournal(SqliteJournal):
        def claim(self, *args):
            if stage == 'claim': raise RuntimeError('private-secret')
            return super().claim(*args)
        def commit(self, *args): raise RuntimeError('private-secret')
    def resolve(args, context):
        if stage == 'resolver': raise RuntimeError('private-secret')
        return 'business-key'
    journal = FailedJournal(tmp_path / 'journal.sqlite')
    tool = definition(lambda args, ctx: effects.append('effect') or {}, operation_key=resolve)
    reason = 'tool_identity_conflict' if stage == 'resolver' else 'tool_journal_failed'
    with pytest.raises(StructuredToolError, match='^' + reason + '$'): dispatch(tool, journal)
    assert effects == (['effect'] if stage == 'commit' else [])
    if stage == 'commit':
        assert SqliteJournal(journal.path).claim('tenant', 'business-key',
            journal.signature('tenant', 'business-key')).state == 'uncertain'


@pytest.mark.parametrize('stop', ['cancel', 'deadline'])
def test_native_post_effect_boundary_commits_and_retains_usage(server, tmp_path, stop, monkeypatch):
    url, requests, responses = server
    effects = []
    config, original = setup(url, effects)
    clock = time.monotonic
    monkeypatch.setattr(time, 'monotonic', lambda: clock() + (1000 if effects and stop == 'deadline' else 0))
    tool = CustomTool('lookup_catalog', 'Lookup', original.parameters,
        lambda args, ctx: effects.append(ctx.operation_key) or {'found': True}, 'v2',
        with_context=True, operation_key=lambda args, ctx: 'business-key')
    journal = SqliteJournal(tmp_path / 'journal.sqlite')
    responses.extend([completion('', [call()]), completion('unused')])
    result = ProsaicRuntime(config, custom_tools={'lookup_catalog': tool}).run(artifact(['lookup_catalog']),
        policy=RunPolicy(allowed_tools=frozenset({'lookup_catalog'})), tool_journal=journal,
        operation_context=InvocationScope('attempt', operation_namespace='tenant'),
        cancelled=lambda: bool(effects) and stop == 'cancel')
    assert result.exit_code == (130 if stop == 'cancel' else 1)
    assert result.metadata['failure_reason'] == ('cancelled' if stop == 'cancel' else 'invocation_timeout')
    assert result.token_usage == 7 and len(requests) == 1
    assert journal.claim('tenant', 'business-key', journal.signature('tenant', 'business-key')).state == 'replay'


def test_journal_replays_count_toward_native_tool_budget(server, tmp_path):
    url, requests, responses = server
    effects = []
    config, original = setup(url, effects)
    tool = CustomTool('lookup_catalog', 'Lookup', original.parameters,
        lambda args, ctx: effects.append('effect') or {}, 'v2', with_context=True,
        operation_key=lambda args, ctx: 'business-key')
    runtime = ProsaicRuntime(config, custom_tools={'lookup_catalog': tool})
    options = {'tool_journal': SqliteJournal(tmp_path / 'journal.sqlite'),
               'operation_context': InvocationScope('attempt', operation_namespace='tenant')}
    responses.extend([completion('', [call()]), completion('done')])
    assert runtime.run(artifact(['lookup_catalog']),
        policy=RunPolicy(allowed_tools=frozenset({'lookup_catalog'})), **options).exit_code == 0
    responses.extend([completion('', [call(), call()]), completion('unused')])
    result = runtime.run(artifact(['lookup_catalog']),
        policy=RunPolicy(allowed_tools=frozenset({'lookup_catalog'}), max_tool_calls=1), **options)
    assert result.metadata['failure_reason'] == 'tool_call_limit'
    assert result.metadata['invocation_budgets_v1']['tool_calls'] == 1
    assert effects == ['effect'] and len(requests) == 3


def test_operation_scope_is_independent_of_accounting_attribution(server, tmp_path):
    url, requests, responses = server
    contexts = []
    config, original = setup(url, [])
    tool = CustomTool('lookup_catalog', 'Lookup', original.parameters,
        lambda args, ctx: contexts.append(ctx) or {}, 'v2', with_context=True,
        operation_key=lambda args, ctx: 'business-key')
    recorder = MemoryRecorder()
    responses.extend([completion('', [call()]), completion('done')])
    result = ProsaicRuntime(config, custom_tools={'lookup_catalog': tool}, accounting=recorder).run(
        artifact(['lookup_catalog']), policy=RunPolicy(allowed_tools=frozenset({'lookup_catalog'})),
        context=ExecutionContext(tenant_id='billing-tenant', invocation_id='billing-invocation'),
        operation_context=InvocationScope('operation-attempt', operation_namespace='business-namespace'),
        tool_journal=SqliteJournal(tmp_path / 'journal.sqlite'))
    assert result.exit_code == 0 and len(requests) == 2
    assert contexts[0].scope.invocation_id == 'operation-attempt'
    assert contexts[0].scope.operation_namespace == 'business-namespace'
    assert result.metadata['accounting_v1']['context']['tenant_id'] == 'billing-tenant'
    assert result.metadata['accounting_v1']['context']['invocation_id'] == 'billing-invocation'
    assert all(intent['context']['invocation_id'] == 'billing-invocation' for intent in recorder.intents.values())
    assert 'business-namespace' not in json.dumps(requests) + json.dumps(list(recorder.intents.values()))
