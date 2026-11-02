"""Host completion must eliminate acknowledgement inference without losing evidence."""
from dataclasses import replace
import hashlib
import json
import time
from types import SimpleNamespace

import pytest
import prosaic_runtime as sdk
from support.conversation_server import turn_fixture
from test_conversation_tool_batches import queue_json, queue_stream, wire_call


def register(fx, handler=None, **options):
    fx.tools['finish'] = sdk.CustomTool('finish', 'Finish the admitted task',
        {'type': 'object', 'additionalProperties': False},
        handler or (lambda a: sdk.TerminalCompletion({'complete': True}, artifact={'rows': [7]})),
        'v2', terminal=True, **options)
    return replace(fx.policy, allowed_terminal_tools=frozenset({'finish'}))


def test_completion_owns_output_and_artifact():
    assert hasattr(sdk, 'TerminalCompletion'), 'public host completion type is missing'
    output, artifact = {'complete': True}, {'rows': [7]}
    value = sdk.TerminalCompletion(output, artifact=artifact)
    output.clear(); artifact['rows'].append(9)
    assert value.output == {'complete': True} and value.artifact == {'rows': [7]}
    value.artifact['rows'].append(10)
    assert value.artifact == {'rows': [7]}
    with pytest.raises(TypeError):
        sdk.TerminalCompletion({'complete': True})


@pytest.mark.parametrize('value', [object(), float('nan'), '\ud800', {'x': float('inf')}, {'x': 'v' * 8_388_609}])
def test_completion_rejects_invalid_or_unbounded_json(value):
    with pytest.raises((ValueError, TypeError)):
        sdk.TerminalCompletion({'complete': True}, artifact=value)


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
@pytest.mark.parametrize('streaming', [False, True])
def test_terminal_tool_returns_real_artifact_without_acknowledgement_call(turn_fixture, tmp_path, provider, streaming):
    with turn_fixture(provider) as fx:
        effects, events, observations = [], [], []
        policy = register(fx, lambda a: effects.append(1) or
            sdk.TerminalCompletion({'complete': True}, artifact={'rows': [7], 'season': '2024/2025'}))
        fx.add_tool('prerequisite', lambda a: {'ready': True})
        policy = replace(policy, allowed_tools=frozenset(fx.tools))
        queue = queue_stream if streaming else queue_json
        queue(fx, [wire_call('read1', 'prerequisite')]); queue(fx, [wire_call('done1', 'finish')])
        fx.queue_text('Malformed prose followed by {"complete":true}')
        result = fx.runtime.run(fx.agent, cwd=tmp_path, policy=policy,
            operation_context=sdk.InvocationScope('attempt', run_id='run', step_id='step'),
            on_event=events.append, observer=observations.append)
        assert result.exit_code == 0 and json.loads(result.stdout) == {'complete': True}
        assert len(fx.requests) == 2 and effects == [1] and len(fx.responses) == 1
        assert result.token_usage == 10
        assert result.metadata['invocation_budgets_v1'] == dict(provider_requests=2, tool_calls=2,
            reported_tokens=10, usage_complete=True)
        evidence = result.metadata['terminalCompletion']
        assert evidence['outputSha256'] == hashlib.sha256(b'{"complete":true}').hexdigest()
        assert evidence['artifactIncluded'] is True
        assert evidence['artifact'] == {'rows': [7], 'season': '2024/2025'}
        assert evidence['invocationId'] == 'attempt' and evidence['toolName'] == 'finish'
        assert evidence['toolVersion'] == 'v2' and evidence['toolCallId'] == 'done1' and evidence['turn'] == 2
        completed = [e for e in events if e['event'] == 'tool_completed']
        assert completed[-1]['terminalCompletion'] == {k: v for k, v in evidence.items() if k != 'artifact'}
        assert '2024/2025' not in json.dumps(events) + json.dumps(observations)
        assert all('terminalCompletion' not in e for e in observations)


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_terminal_model_acknowledgement_is_not_completion(turn_fixture, tmp_path, provider):
    with turn_fixture(provider) as fx:
        policy = register(fx)
        fx.queue_text('{"complete":true}')
        result = fx.runtime.run(fx.agent, cwd=tmp_path, policy=policy)
        assert result.exit_code == 1 and result.metadata['failure_reason'] == 'terminal_completion_missing'
        assert len(fx.requests) == 1 and 'terminalCompletion' not in result.metadata


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('calls', [
    [wire_call('a', 'finish'), wire_call('b', 'ordinary')],
    [wire_call('a', 'ordinary'), wire_call('b', 'finish')],
    [wire_call('a', 'ordinary'), wire_call('a', 'ordinary')],
    [wire_call('a', 'ordinary'), wire_call('b', 'missing')],
])
@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_whole_batch_admission_prevents_sibling_effects(turn_fixture, tmp_path, provider, streaming, calls):
    with turn_fixture(provider) as fx:
        effects = []
        policy = register(fx, lambda a: effects.append('finish'))
        fx.add_tool('ordinary', lambda a: effects.append('ordinary'))
        policy = replace(policy, allowed_tools=frozenset(fx.tools))
        (queue_stream if streaming else queue_json)(fx, calls)
        result = fx.runtime.run(fx.agent, cwd=tmp_path, policy=policy)
        assert result.exit_code != 0 and effects == [] and len(fx.requests) == 1
        assert result.metadata['invocation_budgets_v1']['tool_calls'] == 0


@pytest.mark.parametrize('mode', ['missing_grant', 'unknown_grant', 'nonterminal_grant', 'journal'])
def test_bad_terminal_authority_rejected_before_provider_or_handler(turn_fixture, tmp_path, mode):
    with turn_fixture('openai-compatible') as fx:
        effects = []
        opts = dict(with_context=True, operation_key=lambda a, c: 'key') if mode == 'journal' else {}
        policy = register(fx, lambda *a: effects.append(1), **opts)
        if mode == 'missing_grant': policy = fx.policy
        elif mode == 'unknown_grant':
            policy = replace(policy, allowed_tools=policy.allowed_tools | {'unknown'}, allowed_terminal_tools={'unknown'})
        elif mode == 'nonterminal_grant':
            fx.add_tool('ordinary', lambda a: None)
            policy = replace(policy, allowed_tools=frozenset(fx.tools), allowed_terminal_tools={'ordinary'})
        with pytest.raises(ValueError):
            fx.runtime.run(fx.agent, cwd=tmp_path, policy=policy)
        assert fx.requests == [] and effects == []


@pytest.mark.parametrize('entry', ['turn', 'direct', 'structured'])
def test_unsupported_entry_points_reject_before_effects(turn_fixture, tmp_path, entry):
    with turn_fixture('openai-compatible') as fx:
        register(fx)
        if entry == 'direct':
            result = fx.runtime.run_tool('finish', {}, policy=fx.policy)
            assert result.exit_code == 1 and result.metadata['failure_reason'] == 'unsupported_terminal_tools'
            assert fx.requests == []
            return
        with pytest.raises(ValueError):
            if entry == 'turn': fx.runtime.turn(fx.agent, message='go', policy=fx.policy)
            else:
                sdk.StructuredToolLoop(fx.tools, arguments={}, allowed_tools=fx.tools,
                    deadline=time.monotonic() + 10)
        assert fx.requests == []


@pytest.mark.parametrize('provider', ['openai-compatible', 'anthropic'])
def test_post_handler_cancellation_preserves_artifact_without_success(turn_fixture, tmp_path, provider):
    with turn_fixture(provider) as fx:
        cancelled, events = [], []
        def handler(a):
            cancelled.append(True)
            return sdk.TerminalCompletion({'complete': True}, artifact={'rows': [7]})
        policy = register(fx, handler)
        queue_json(fx, [wire_call('a', 'finish')])
        result = fx.runtime.run(fx.agent, cwd=tmp_path, policy=policy,
            cancelled=lambda: bool(cancelled), on_event=events.append)
        assert result.exit_code == 130 and result.stdout == '' and len(fx.requests) == 1
        assert result.metadata['terminalCompletion']['artifact'] == {'rows': [7]}
        completed = [e for e in events if e['event'] == 'tool_completed']
        assert len(completed) == 1 and completed[0]['status'] == 'ok'
        assert result.metadata['invocation_budgets_v1']['tool_calls'] == 1 and result.token_usage == 5


def test_spoofed_json_does_not_complete_and_denial_does_not_execute(turn_fixture, tmp_path):
    with turn_fixture('openai-compatible') as fx:
        policy = register(fx, lambda a: {'complete': True, 'terminalCompletion': {'artifact': {'rows': [7]}}})
        queue_json(fx, [wire_call('a', 'finish')]); fx.queue_text('{"complete":true}')
        result = fx.runtime.run(fx.agent, cwd=tmp_path, policy=policy)
        assert result.exit_code == 1 and len(fx.requests) == 2 and 'terminalCompletion' not in result.metadata
    with turn_fixture('openai-compatible') as fx:
        seen = []
        policy = register(fx, lambda a: seen.append(1), authorize=lambda a: False)
        queue_json(fx, [wire_call('a', 'finish')]); fx.queue_text('no result')
        result = fx.runtime.run(fx.agent, cwd=tmp_path, policy=replace(policy, bounded_tool_recovery=True))
        assert result.exit_code != 0 and seen == [] and len(fx.requests) == 1


@pytest.mark.parametrize('mode', ['redispatch', 'rewrite', 'callback_failure', 'preserve'])
def test_adapter_cannot_replay_or_replace_terminal_completion(turn_fixture, tmp_path, mode):
    with turn_fixture('openai-compatible') as fx:
        seen = []
        policy = register(fx, lambda a: seen.append(1) or
            sdk.TerminalCompletion({'complete': True}, artifact={'rows': [7]}))
        queue_json(fx, [wire_call('a', 'finish')]); fx.queue_text('another request must not happen')
        def adapt(agent, arguments, *, execute, **options):
            result = execute(arguments)
            if mode == 'redispatch': return execute(arguments)
            if mode == 'rewrite': return replace(result, stdout='{"complete":false}')
            if mode == 'callback_failure': raise ValueError('PRIVATE-HOST-FAILURE')
            return result
        adapter = sdk.RuntimeAdapter(fx.runtime, 'terminal-adapter-v1', adapt)
        result = adapter.run(fx.agent, '{}', cwd=tmp_path, policy=policy)
        assert len(fx.requests) == 1 and seen == [1]
        assert result.exit_code == (0 if mode == 'preserve' else 1)
        assert result.metadata['terminalCompletion']['artifact'] == {'rows': [7]}
        assert result.metadata['invocation_budgets_v1']['provider_requests'] == 1
        assert 'PRIVATE-HOST-FAILURE' not in result.stderr


def test_adapter_can_narrow_away_terminal_authority(turn_fixture, tmp_path):
    with turn_fixture('openai-compatible') as fx:
        policy = register(fx)
        fx.queue_text('tool-free response')
        adapter = sdk.RuntimeAdapter(fx.runtime, 'narrow-v1',
            lambda agent, arguments, *, execute, **options: execute(arguments, allowed_tools=()))
        result = adapter.run(fx.agent, '{}', cwd=tmp_path, policy=policy)
        assert result.exit_code == 0 and len(fx.requests) == 1
        assert not fx.requests[0].get('tools') and 'terminalCompletion' not in result.metadata


def test_combined_terminal_limit_and_critical_hook_are_failures(turn_fixture, tmp_path):
    with turn_fixture('openai-compatible') as fx:
        policy = register(fx, max_result_bytes=100)
        queue_json(fx, [wire_call('a', 'finish')]); fx.queue_text('must not run')
        result = fx.runtime.run(fx.agent, cwd=tmp_path, policy=policy)
        assert result.exit_code == 1 and result.metadata['failure_reason'] == 'terminal_completion_limit'
        assert len(fx.requests) == 1 and result.token_usage == 5
    with turn_fixture('openai-compatible') as fx:
        policy = register(fx)
        queue_json(fx, [wire_call('a', 'finish')]); fx.queue_text('must not run')
        def event_hook(event):
            if event['event'] == 'tool_completed': raise RuntimeError('PRIVATE-HOOK-FAILURE')
        result = fx.runtime.run(fx.agent, cwd=tmp_path, policy=policy, on_event=event_hook)
        assert result.exit_code == 1 and result.metadata['failure_reason'] == 'critical_hook_error'
        assert result.metadata['terminalCompletion']['artifact'] == {'rows': [7]}
        assert len(fx.requests) == 1 and 'PRIVATE-HOOK-FAILURE' not in result.stderr


@pytest.mark.parametrize('kind', ['invalid_artifact', 'callback_failure', 'denied'])
def test_terminal_handler_failures_cannot_request_model_repair(turn_fixture, tmp_path, kind):
    with turn_fixture('openai-compatible') as fx:
        def handler(a):
            if kind == 'callback_failure': raise RuntimeError('private callback error')
            return sdk.TerminalCompletion({'complete': True}, artifact=object())
        policy = register(fx, handler, authorize=(lambda a: False) if kind == 'denied' else None)
        queue_json(fx, [wire_call('a', 'finish')]); fx.queue_text('must not run')
        result = fx.runtime.run(fx.agent, cwd=tmp_path, policy=policy)
        assert result.exit_code == 1 and len(fx.requests) == 1
        assert 'terminalCompletion' not in result.metadata


@pytest.mark.parametrize('kind', ['callback_failure', 'limit'])
def test_adapter_cannot_repeat_failed_terminal_effect(turn_fixture, tmp_path, kind):
    with turn_fixture('openai-compatible') as fx:
        seen = []
        def handler(a):
            seen.append(1)
            if kind == 'callback_failure': raise RuntimeError('uncertain effect')
            return sdk.TerminalCompletion({'complete': True}, artifact={'rows': [7]})
        policy = register(fx, handler, **({'max_result_bytes': 100} if kind == 'limit' else {}))
        queue_json(fx, [wire_call('a', 'finish')]); queue_json(fx, [wire_call('b', 'finish')])
        def adapt(agent, arguments, *, execute, **options):
            execute(arguments)
            return execute(arguments)
        result = sdk.RuntimeAdapter(fx.runtime, 'failed-v1', adapt).run(fx.agent, '{}', cwd=tmp_path, policy=policy)
        assert result.exit_code == 1 and seen == [1] and len(fx.requests) == 1
        assert result.metadata['invocation_budgets_v1']['tool_calls'] == 1


def test_post_handler_deadline_retains_known_completion(turn_fixture, tmp_path, monkeypatch):
    offset = [0]
    monkeypatch.setattr('prosaic_runtime.runtime.time',
        SimpleNamespace(monotonic=lambda: time.monotonic() + offset[0]))
    with turn_fixture('openai-compatible') as fx:
        def handler(a):
            offset[0] = 10_000
            return sdk.TerminalCompletion({'complete': True}, artifact={'rows': [7]})
        policy = register(fx, handler); queue_json(fx, [wire_call('a', 'finish')])
        result = fx.runtime.run(fx.agent, cwd=tmp_path, policy=policy)
        assert result.exit_code == 1 and result.timed_out and result.stdout == ''
        assert result.metadata['terminalCompletion']['artifact'] == {'rows': [7]}
        assert len(fx.requests) == 1 and result.token_usage == 5


def test_terminal_completion_preserves_unknown_usage(turn_fixture, tmp_path):
    with turn_fixture('openai-compatible') as fx:
        policy = register(fx); queue_json(fx, [wire_call('a', 'finish')])
        status, kind, body = fx.responses[0]
        document = json.loads(body); document.pop('usage')
        fx.responses[0] = status, kind, json.dumps(document).encode()
        result = fx.runtime.run(fx.agent, cwd=tmp_path, policy=policy)
        assert result.exit_code == 0 and result.token_usage is None
        usage = result.metadata['invocation_budgets_v1']
        assert usage['provider_requests'] == usage['tool_calls'] == 1 and usage['usage_complete'] is False


def test_terminal_journal_denied_even_with_ready_host_journal(turn_fixture, tmp_path):
    class Journal:
        identity = 'host-journal'
        contract_version = 'tool-journal-v1'
        def claim(self, *args): pytest.fail('journal claim before admission')
        def commit(self, *args): pytest.fail('journal commit before admission')
    with turn_fixture('openai-compatible') as fx:
        policy = register(fx, with_context=True, operation_key=lambda a, c: 'key')
        with pytest.raises(ValueError, match='journaled terminal'):
            fx.runtime.run(fx.agent, cwd=tmp_path, policy=policy, tool_journal=Journal(),
                operation_context=sdk.InvocationScope('op', operation_namespace='host'))
        assert fx.requests == []


def test_terminal_json_depth_cycles_and_nodes_are_bounded():
    cycle = []; cycle.append(cycle)
    for artifact in (cycle, [0] * 65_537):
        with pytest.raises(ValueError): sdk.TerminalCompletion({}, artifact=artifact)


def test_binding_overhead_limit_retains_failed_invocation_evidence(turn_fixture, tmp_path, monkeypatch):
    monkeypatch.setattr('prosaic_runtime.terminal.MAX_BYTES', 800)
    with turn_fixture('openai-compatible') as fx:
        policy = register(fx, lambda a: sdk.TerminalCompletion({}, artifact={'data': 'x' * 500}))
        queue_json(fx, [wire_call('a', 'finish')])
        result = fx.runtime.run(fx.agent, cwd=tmp_path, policy=policy)
        assert result.exit_code == 1 and result.metadata['failure_reason'] == 'terminal_completion_limit'
        assert result.metadata['terminalCompletion']['artifactIncluded'] is False
        assert result.token_usage == 5 and len(fx.requests) == 1


@pytest.mark.parametrize('event_name', ['tool_completed', 'completed'])
@pytest.mark.parametrize('boundary', ['cancelled', 'deadline'])
def test_completion_hooks_cannot_return_success_after_resource_expiry(turn_fixture, tmp_path, monkeypatch, event_name, boundary):
    offset, cancelled = [0], []
    monkeypatch.setattr('prosaic_runtime.runtime.time',
        SimpleNamespace(monotonic=lambda: time.monotonic() + offset[0]))
    with turn_fixture('openai-compatible') as fx:
        policy = register(fx); queue_json(fx, [wire_call('a', 'finish')])
        def hook(event):
            if event['event'] == event_name:
                if boundary == 'cancelled': cancelled.append(True)
                else: offset[0] = 10_000
        result = fx.runtime.run(fx.agent, cwd=tmp_path, policy=policy,
            on_event=hook, cancelled=lambda: bool(cancelled))
        assert result.exit_code == (130 if boundary == 'cancelled' else 1)
        assert result.timed_out == (boundary == 'deadline') and result.stdout == ''
        assert result.metadata['terminalCompletion']['artifact'] == {'rows': [7]}
        assert result.token_usage == 5 and len(fx.requests) == 1


@pytest.mark.parametrize('version', ['v2\n', '\x00v2', 'é' * 65])
def test_incompatible_terminal_version_is_rejected_before_effects(turn_fixture, tmp_path, version):
    with turn_fixture('openai-compatible') as fx:
        seen = []
        with pytest.raises(ValueError):
            fx.tools['finish'] = sdk.CustomTool('finish', 'Finish',
                {'type': 'object', 'additionalProperties': False}, lambda a: seen.append(1),
                version, terminal=True)
            fx.runtime.run(fx.agent, cwd=tmp_path,
                policy=replace(fx.policy, allowed_terminal_tools={'finish'}))
        assert seen == [] and fx.requests == []


def test_repeated_large_strings_are_bounded_before_encoding(monkeypatch):
    import prosaic_runtime.terminal as terminal
    def serializer_must_not_run(*args, **kwargs):
        pytest.fail('oversized aggregate string payload reached JSON serialization')
    monkeypatch.setattr(terminal.json, 'dumps', serializer_must_not_run)
    with pytest.raises(ValueError):
        sdk.TerminalCompletion({}, artifact=['x' * 1_000_000] * 20)


def test_large_integer_aliases_are_bounded_before_encoding(monkeypatch):
    import prosaic_runtime.terminal as terminal
    monkeypatch.setattr(terminal.json, 'dumps', lambda *a, **k: pytest.fail('oversized integer payload reached encoding'))
    with pytest.raises(ValueError):
        sdk.TerminalCompletion({}, artifact=[10**2000] * 5000)
