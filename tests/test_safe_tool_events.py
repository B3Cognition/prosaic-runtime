import json
import pytest
import prosaic_runtime as api
from prosaic_runtime.events import event_context
from prosaic_runtime.tools import execute_custom_tool
from support.tool_journal import SqliteJournal

SCOPE = api.InvocationScope('attempt', run_id='run', step_id='node', operation_namespace='tenant')
SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['component'],
          'properties': {'component': {'type': 'string'}, 'secret': {'type': 'string'}}}


def policy():
    return api.ToolEventPolicyV1('1', {'component': api.ToolDisplayFieldV1('arguments', '/component'),
        'count': api.ToolDisplayFieldV1('result', '/count')})


def bindings(grant=lambda use: True):
    return api.ToolContractBindingsV1('host', '1', SCOPE,
        display_authorizer=api.ToolDisplayAuthorizerV1('viewer', '1', grant))


def execute(tool, args=None, *, grant=lambda use: True, journal=None):
    events = []
    with event_context(events.append):
        result = execute_custom_tool(tool, json.dumps(args or {'component': 'frontend', 'secret': 'NEVER_DISPLAY'}),
            check_boundary=lambda: None, scope=SCOPE, tool_journal=journal,
            tool_contract_bindings=bindings(grant))
    return result, events


def test_events_only_keeps_legacy_return():
    order = []
    tool = api.CustomTool('lookup', 'Lookup', SCHEMA, lambda a: order.append('handler') or {'count': 14, 'secret': 'NEVER_DISPLAY'},
        '1', event_policy=policy(), authorize=lambda a: order.append('authorized') or True)
    result, events = execute(tool, grant=lambda use: order.append(use.phase) or True)
    assert result['result']['count'] == 14 and order == ['authorized', 'started', 'handler', 'completed']
    assert [e['event'] for e in events] == ['tool_started', 'tool_completed']
    assert events[0]['toolEventV1']['display'] == {'component': 'frontend'}
    assert events[1]['toolEventV1']['display'] == {'component': 'frontend', 'count': 14}
    assert 'NEVER_DISPLAY' not in json.dumps(events)
    assert events[0]['call_id'] == events[1]['call_id']


@pytest.mark.parametrize('grant', [lambda use: False, lambda use: 1, lambda use: (_ for _ in ()).throw(Exception('NEVER_DISPLAY'))])
def test_display_grant_is_separate_from_execution(grant):
    tool = api.CustomTool('lookup', 'Lookup', SCHEMA, lambda a: {'count': 14}, '1', event_policy=policy())
    result, events = execute(tool, grant=grant)
    assert result['status'] == 'ok'
    assert all(e['toolEventV1'].get('display', {}) == {} for e in events)


@pytest.mark.parametrize('invalid', [False, True])
def test_denied_or_invalid_call_has_no_argument_display(invalid):
    tool = api.CustomTool('lookup', 'Lookup', SCHEMA, lambda a: pytest.fail('handler'), '1',
        event_policy=policy(), authorize=lambda a: False)
    result, events = execute(tool, {'component': 2} if invalid else None)
    assert result['status'] == 'error' and len(events) == 1
    assert events[0]['toolEventV1']['phase'] == 'rejected' and events[0]['toolEventV1'].get('display', {}) == {}


def test_journal_replay_never_reports_new_start(tmp_path):
    calls = []
    journal = SqliteJournal(tmp_path / 'j.sqlite')
    tool = api.CustomTool('lookup', 'Lookup', SCHEMA, lambda a, c: calls.append(1) or {'count': 14},
        '1', event_policy=policy(), with_context=True, operation_key=lambda a, c: 'key')
    first, events = execute(tool, journal=journal)
    second, replay = execute(tool, journal=journal)
    assert first == second and calls == [1]
    assert len(events) == 2 and len(replay) == 1 and replay[0]['toolEventV1']['phase'] == 'replayed'


@pytest.mark.parametrize('value,display,unavailable', [('x\x1b[31m', {}, False), ('\u202eabc', {}, False),
    ('x' * 257, {}, True), ({'private': 1}, {}, False), ('safe', {'component': 'safe'}, False)])
def test_bad_projection_does_not_retry_effect(value, display, unavailable):
    calls = []
    tool = api.CustomTool('lookup', 'Lookup', {'type': 'object', 'additionalProperties': False, 'properties': {'component': {}}}, lambda a: calls.append(1) or {}, '1', event_policy=policy())
    result, events = execute(tool, {'component': value})
    assert result['status'] == 'ok' and calls == [1]
    assert events[0]['toolEventV1'].get('display', {}) == display
    assert events[0]['toolEventV1'].get('displayUnavailable', False) is unavailable


def test_each_attempt_has_one_completion():
    tool = api.CustomTool('lookup', 'Lookup', SCHEMA, lambda a: 1 / 0, '1', event_policy=policy())
    result, events = execute(tool)
    assert result['error'] == 'handler_error'
    assert sum(e['event'] == 'tool_completed' for e in events) == 1


def test_direct_events_share_safe_identity_and_payload_free_observer():
    from test_direct_tool import runtime, POLICY
    events, observed = [], []
    rt = runtime(lambda a: {'count': 14}, event_policy=api.ToolEventPolicyV1('1', {
        'count': api.ToolDisplayFieldV1('result', '/count')}))
    result = rt.run_tool('lookup', {'value': 2}, policy=POLICY, on_event=events.append,
        observer=observed.append, tool_contract_bindings=bindings())
    assert result.exit_code == 0 and len(events) == 2
    assert events[1]['toolEventV1']['display'] == {'count': 14}
    assert events[0]['call_id'] == events[1]['call_id'] and events[0]['call_id'] != 'direct-0'
    assert all('toolEventV1' not in event and 'count' not in json.dumps(event) for event in observed)


def test_structured_events_share_executor_without_duplicate_pairs():
    import time
    from prosaic_runtime.structured_tools import StructuredToolLoop, ToolRequest
    events = []
    tool = api.CustomTool('lookup', 'Lookup', SCHEMA, lambda a: {'count': 14}, '1', event_policy=policy())
    loop = StructuredToolLoop({'lookup': tool}, arguments={}, deadline=time.monotonic() + 30,
        allowed_tools={'lookup'}, on_event=events.append, tool_contract_bindings=bindings())
    assert loop.dispatch(ToolRequest('lookup', {'component': 'frontend'}))[1] == {'count': 14}
    assert [e['event'] for e in events] == ['tool_started', 'tool_completed']
    assert events[1]['toolEventV1']['display']['count'] == 14
