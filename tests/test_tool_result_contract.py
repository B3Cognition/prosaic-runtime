import pytest
import prosaic_runtime as api
from prosaic_runtime.tools import execute_custom_tool
from prosaic_runtime.tool_effects import ToolEffectFailure
from support.tool_journal import SqliteJournal

SCHEMA = {'type': 'object', 'additionalProperties': False, 'properties': {}}
OUTPUT = {'type': 'object', 'required': ['count'], 'additionalProperties': False,
          'properties': {'count': {'type': 'integer'}}}


def definition(handler, *, journaled=False, limit=65536):
    return api.CustomTool('lookup', 'Lookup', SCHEMA, handler, 'v2',
        result_contract=api.ToolResultContractV1('1', OUTPUT),
        with_context=journaled, operation_key=(lambda a, c: 'key') if journaled else None,
        max_result_bytes=limit)


def value(count=14):
    return api.ToolResultV1(data={'count': count}, evidence=(
        api.EvidenceDraftV1('graph', 'revision-42', 'component:x', '/count', 'Dependents'),))


def dispatch(tool, journal=None, boundary=lambda: None, invocation='i'):
    return execute_custom_tool(tool, '{}', check_boundary=boundary,
        scope=api.InvocationScope(invocation, operation_namespace='tenant'), tool_journal=journal)


def test_output_schema_checks_data_before_commit(tmp_path):
    journal = SqliteJournal(tmp_path / 'journal.sqlite')
    with pytest.raises(ToolEffectFailure, match='tool_effect_uncertain'):
        dispatch(definition(lambda a, c: value('invalid'), journaled=True), journal)
    assert journal.claim('tenant', 'key', journal.signature('tenant', 'key')).state == 'uncertain'


def test_replay_validates_pinned_contract_and_keeps_source_revision(tmp_path):
    journal = SqliteJournal(tmp_path / 'journal.sqlite')
    calls = []
    tool = definition(lambda a, c: calls.append(c.scope.invocation_id) or value(), journaled=True)
    first = dispatch(tool, journal, invocation='first')
    second = dispatch(tool, journal, invocation='second')
    assert first == second and calls == ['first']
    assert first['result']['sources'][0]['sourceRevision'] == 'revision-42'
    assert 'ev_' not in str(first)
    assert first['status'] == 'ok' and first['result']['data'] == {'count': 14}


def test_invalid_journaled_result_preserves_uncertainty(tmp_path):
    journal = SqliteJournal(tmp_path / 'journal.sqlite')
    calls = []
    tool = definition(lambda a, c: calls.append(1) or value(), journaled=True, limit=40)
    for _ in range(2):
        with pytest.raises(ToolEffectFailure, match='tool_effect_uncertain'):
            dispatch(tool, journal)
    assert calls == [1]


def test_commit_precedes_later_cancellation(tmp_path):
    from prosaic_runtime.events import Cancelled
    journal = SqliteJournal(tmp_path / 'journal.sqlite')
    completed = []
    tool = definition(lambda a, c: completed.append(1) or value(), journaled=True)
    def boundary():
        if completed: raise Cancelled()
    with pytest.raises(Cancelled): dispatch(tool, journal, boundary)
    assert dispatch(tool, journal)['status'] == 'ok'
    assert completed == [1]


def test_invalid_unjournaled_data_and_unregistered_typed_values_are_not_trusted():
    assert dispatch(definition(lambda a: value('bad')))['error'] == 'invalid_result'
    plain = api.CustomTool('lookup', 'Lookup', SCHEMA, lambda a: value(), 'v1')
    assert dispatch(plain)['error'] == 'invalid_result'
    assert dispatch(definition(lambda a: {'version': 1, 'data': {'count': 14}, 'sources': []}))['error'] == 'invalid_result'


def test_bad_evidence_pointer_is_rejected():
    result = api.ToolResultV1(data={'count': 14}, evidence=(api.EvidenceDraftV1('graph', None, '', '/missing', 'Missing'),))
    assert dispatch(definition(lambda a: result))['error'] == 'invalid_result'


def test_controlled_failure_keeps_legacy_outer_frame():
    assert dispatch(definition(lambda a: api.ToolFailure('missing'))) == {'status': 'error', 'error': 'missing'}
