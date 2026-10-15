"""A proven callback failure is durable; unknown effects remain fenced."""
import json

import pytest
from prosaic_runtime import CustomTool, InvocationScope, ProsaicRuntime, RunPolicy
from support.tool_journal import SqliteJournal
from test_custom_tool_transport import setup, call
from test_runtime import server, artifact, completion


def test_controlled_failure_is_committed_and_replayed_without_callback(server, tmp_path):
    from prosaic_runtime import ToolFailure
    url, requests, responses = server
    seen = []
    config, original = setup(url, seen)
    tool = CustomTool('lookup_catalog', 'Lookup', original.parameters,
        lambda args, context: seen.append(context.operation_key) or ToolFailure('read_unavailable'),
        'v2', with_context=True, operation_key=lambda args, context: 'original-request')
    journal = SqliteJournal(tmp_path / 'journal.sqlite')
    runtime = ProsaicRuntime(config, custom_tools={'lookup_catalog': tool})
    for attempt in ('first', 'second'):
        responses.extend([completion('', [call()]), completion('Read unavailable')])
        result = runtime.run(artifact(['lookup_catalog']),
            policy=RunPolicy(allowed_tools=frozenset({'lookup_catalog'})), tool_journal=journal,
            operation_context=InvocationScope(attempt, operation_namespace='tenant'))
        assert result.exit_code == 0
    assert seen == ['original-request']
    outcomes = [json.loads(m['content']) for r in requests for m in r['messages'] if m['role'] == 'tool']
    assert outcomes == [{'status': 'error', 'error': 'read_unavailable'}] * 2


@pytest.mark.parametrize('code', ['', 'private error text', 'x' * 65, 1, 'line\nbreak'])
def test_controlled_failure_accepts_only_bounded_machine_codes(code):
    from prosaic_runtime import ToolFailure
    with pytest.raises(ValueError):
        ToolFailure(code)


from support.conversation_server import turn_fixture


@pytest.mark.parametrize('boundary', ['complete', 'cancel_after_commit', 'lost_commit_ack'])
def test_controlled_failure_evidence_obeys_durable_commit_boundary(turn_fixture, tmp_path, boundary):
    from prosaic_runtime import ToolFailure
    cancelled, seen = [], []
    class Journal(SqliteJournal):
        def commit(self, *args):
            super().commit(*args)
            if boundary == 'cancel_after_commit': cancelled.append(True)
            if boundary == 'lost_commit_ack': raise RuntimeError('private lost acknowledgement')
    journal = Journal(tmp_path / 'effects.sqlite')
    with turn_fixture('openai-compatible') as fx:
        fx.add_tool('lookup', lambda args, context: seen.append(1) or ToolFailure('read_unavailable'),
            operation_key=lambda args, context: 'original-operation')
        fx.queue_calls([{'id': 'a', 'name': 'lookup', 'arguments': '{}'}])
        if boundary == 'complete': fx.queue_text('Read unavailable')
        result = fx.runtime.turn(fx.agent, message='Find', policy=fx.policy, tool_journal=journal,
            operation_context=InvocationScope('first', operation_namespace='tenant'), cancelled=lambda: bool(cancelled))
        outcome = result.evidence.to_dict()['toolOutcomes'][0]
        assert outcome['status'] == ('uncertain' if boundary == 'lost_commit_ack' else 'completed')
        if boundary == 'lost_commit_ack':
            assert result.result.metadata['failure_reason'] == 'toolJournalFailure'
        else:
            assert outcome['outcome'] == {'status': 'error', 'error': 'read_unavailable'}
        if boundary == 'cancel_after_commit':
            cancelled.clear()
            fx.queue_calls([{'id': 'b', 'name': 'lookup', 'arguments': '{}'}])
            fx.queue_text('Read unavailable')
            replay = fx.runtime.turn(fx.agent, message='Find', policy=fx.policy, tool_journal=journal,
                operation_context=InvocationScope('second', operation_namespace='tenant'))
            assert replay.evidence.to_dict()['toolOutcomes'][0]['status'] == 'replayed'
        assert seen == [1]


def test_unjournaled_controlled_failure_and_ordinary_dictionary_are_distinct(turn_fixture):
    from prosaic_runtime import ToolFailure
    with turn_fixture('openai-compatible') as fx:
        fx.add_tool('lookup', lambda args: ToolFailure('read_unavailable'))
        fx.queue_calls([{'id': 'a', 'name': 'lookup', 'arguments': '{}'}])
        fx.queue_text('Unavailable')
        result = fx.runtime.turn(fx.agent, message='Find', policy=fx.policy)
        assert result.evidence.to_dict()['toolOutcomes'][0]['outcome'] == {
            'status': 'error', 'error': 'read_unavailable'}
        fx.add_tool('lookup', lambda args: {'status': 'error', 'error': 'ordinary_data'})
        fx.queue_calls([{'id': 'b', 'name': 'lookup', 'arguments': '{}'}])
        fx.queue_text('Data')
        result = fx.runtime.turn(fx.agent, message='Find', policy=fx.policy)
        assert result.evidence.to_dict()['toolOutcomes'][0]['outcome'] == {
            'status': 'ok', 'result': {'status': 'error', 'error': 'ordinary_data'}}
