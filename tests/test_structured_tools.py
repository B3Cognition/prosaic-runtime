import json
import time

import pytest
from prosaic_runtime import CustomTool, Result
from prosaic_runtime.structured_tools import Completion, StructuredToolError, StructuredToolLoop, ToolRequest


def loop(handler=lambda a: {'value': a['id']}, **options):
    schema = {'type': 'object', 'properties': {'id': {'type': 'string'}},
              'required': ['id'], 'additionalProperties': False}
    tool = CustomTool('lookup', 'Read value', schema, handler, 'v1', max_result_bytes=512)
    return StructuredToolLoop({'lookup': tool}, allowed_tools={'lookup'}, arguments={'prompt': 'Read'},
                              deadline=time.monotonic() + 10, **options)


def result(value):
    return Result(0, json.dumps(value), '')


def complete(value):
    return Completion(value)


def test_registered_tool_history_and_host_result():
    driver = loop().turns(admit=complete)
    assert json.loads(next(driver))['availableTools'][0]['name'] == 'lookup'
    prompt = driver.send(result({'tool': {'name': 'lookup', 'arguments': {'id': 'one'}}}))
    assert json.loads(prompt)['history'] == [{'tool': {'name': 'lookup', 'arguments': {'id': 'one'}},
                                             'result': {'value': 'one'}}]
    with pytest.raises(StopIteration) as done:
        driver.send(result({'proposal': {'copy': 'Finished'}}))
    assert done.value.value == {'copy': 'Finished'}


@pytest.mark.parametrize('raw', ['{"tool":{},"tool":{}}', '{"proposal":{"x":NaN}}',
                                '{"proposal":{"x":1e999}}', '[]', '{',
                                '{"tool":{"name":"lookup","arguments":{"id":1}}}',
                                '{"tool":{"name":"invented","arguments":{}}}'])
def test_invalid_protocol_never_calls_handler(raw):
    calls = []
    driver = loop(lambda a: calls.append(a)).turns(admit=complete)
    next(driver)
    prompt = driver.send(Result(0, raw, ''))
    assert json.loads(prompt)['history'][-1]['validation']['ok'] is False
    assert not calls
    with pytest.raises(StructuredToolError):
        driver.send(Result(0, raw, ''))


def test_repeated_call_gets_one_correction_without_reexecution():
    calls = []
    driver = loop(lambda a: calls.append(a) or {'value': 'one'}).turns(admit=complete)
    next(driver)
    call = result({'tool': {'name': 'lookup', 'arguments': {'id': 'one'}}})
    driver.send(call)
    prompt = driver.send(call)
    assert 'already observed' in json.loads(prompt)['history'][-1]['validation']['message']
    with pytest.raises(StructuredToolError, match='repeated-tool-call'):
        driver.send(call)
    assert len(calls) == 1


def test_host_can_require_prerequisite_read():
    driver = loop().turns(admit=lambda value: ToolRequest('lookup', {'id': 'one'}))
    next(driver)
    prompt = driver.send(result({'proposal': {}}))
    assert json.loads(prompt)['history'][-1]['result']['value'] == 'one'


def test_default_registration_does_not_grant_tools():
    driver = loop()
    driver.allowed_tools = frozenset()
    turns = driver.turns(admit=complete)
    assert not json.loads(next(turns))['availableTools']
    prompt = turns.send(result({'tool': {'name': 'lookup', 'arguments': {'id': 'one'}}}))
    assert 'validation' in json.loads(prompt)['history'][-1]


def test_cancellation_and_deadline_before_execution():
    for options, expected in [({'cancelled': lambda: True}, 'execution-cancelled'), ({}, 'execution-timeout')]:
        session = loop(**options)
        if not options:
            session.deadline = time.monotonic() - 1
        with pytest.raises(StructuredToolError, match=expected):
            next(session.turns(admit=complete))


def test_bounded_history_and_handler_errors_do_not_leak():
    def fail(args):
        raise RuntimeError('private-secret')
    driver = loop(fail).turns(admit=complete)
    next(driver)
    with pytest.raises(StructuredToolError) as error:
        driver.send(result({'tool': {'name': 'lookup', 'arguments': {'id': 'one'}}}))
    assert 'private-secret' not in str(error.value)
    driver = loop(max_history_bytes=10).turns(admit=complete)
    next(driver)
    with pytest.raises(StructuredToolError, match='history-limit'):
        driver.send(result({'tool': {'name': 'lookup', 'arguments': {'id': 'one'}}}))


def test_model_budget_and_failed_model_do_not_invoke_tools():
    driver = loop(max_turns=1).turns(admit=complete)
    next(driver)
    with pytest.raises(StructuredToolError, match='step-limit'):
        driver.send(result({'tool': {'name': 'lookup', 'arguments': {'id': 'one'}}}))
    driver = loop().turns(admit=complete)
    next(driver)
    with pytest.raises(StructuredToolError, match='execution-failed'):
        driver.send(Result(1, '', 'private-provider-message'))


def test_tool_authorization_and_result_limits_are_enforced():
    schema = {'type': 'object', 'properties': {}, 'additionalProperties': False}
    seen = []
    for authorize, handler, expected in [
        (lambda a: False, lambda a: seen.append(a), 'tool-authorization_denied'),
        (None, lambda a: 'x' * 100, 'tool-result_limit'),
        (None, lambda a: float('inf'), 'tool-invalid_result'),
    ]:
        tool = CustomTool('read_value', 'Read', schema, handler, 'v1',
                          max_result_bytes=64, authorize=authorize)
        session = StructuredToolLoop({'read_value': tool}, arguments={},
                    allowed_tools={'read_value'}, deadline=time.monotonic() + 10)
        driver = session.turns(admit=complete)
        next(driver)
        with pytest.raises(StructuredToolError, match=expected):
            driver.send(result({'tool': {'name': 'read_value', 'arguments': {}}}))
    assert not seen


def test_input_response_limits_and_single_use():
    session = loop(max_input_bytes=10)
    with pytest.raises(StructuredToolError, match='input-limit'):
        next(session.turns(admit=complete))
    session = loop(max_response_bytes=10)
    driver = session.turns(admit=complete)
    next(driver)
    prompt = driver.send(Result(0, 'x' * 11, ''))
    assert 'validation' in json.loads(prompt)['history'][-1]
    with pytest.raises(ValueError, match='single-use'):
        next(session.turns(admit=complete))


def test_cancellation_during_handler_never_admits_result():
    stopped = []
    driver = loop(lambda a: stopped.append(True) or {'value': 'one'},
                  cancelled=lambda: bool(stopped)).turns(admit=complete)
    next(driver)
    with pytest.raises(StructuredToolError, match='execution-cancelled'):
        driver.send(result({'tool': {'name': 'lookup', 'arguments': {'id': 'one'}}}))


def test_product_terminal_decision_and_admission_repair():
    driver = loop().turns(admit=complete, tool_outcome=lambda name, data: Completion({'unavailable': True}))
    next(driver)
    with pytest.raises(StopIteration) as done:
        driver.send(result({'tool': {'name': 'lookup', 'arguments': {'id': 'one'}}}))
    assert done.value.value == {'unavailable': True}

    def admit(value):
        if value.get('valid') is not True:
            raise ValueError('private-validation-text')
        return Completion(value)
    driver = loop().turns(admit=admit, feedback=lambda step: 'Use the verified facts.')
    next(driver)
    prompt = driver.send(result({'proposal': {'valid': False}}))
    assert 'private-validation-text' not in prompt
    assert 'Use the verified facts.' in prompt
    with pytest.raises(StopIteration):
        driver.send(result({'proposal': {'valid': True}}))


def test_explicit_polling_allowance_and_events():
    events, calls = [], []
    driver = loop(lambda a: calls.append(a) or {}, max_identical_calls=2,
                  on_event=events.append).turns(admit=complete)
    next(driver)
    call = result({'tool': {'name': 'lookup', 'arguments': {'id': 'one'}}})
    driver.send(call)
    driver.send(call)
    assert len(calls) == 2
    assert [e['type'] for e in events] == ['tool_start', 'tool_complete'] * 2


@pytest.mark.parametrize('options', [{'max_turns': 0}, {'max_turns': True}, {'max_history_bytes': -1},
                                    {'deadline': float('inf')}, {'allowed_tools': {'invented'}}])
def test_bad_host_configuration_is_rejected(options):
    if 'deadline' in options:
        with pytest.raises(ValueError):
            StructuredToolLoop({}, arguments={}, **options)
    elif 'allowed_tools' in options:
        with pytest.raises(ValueError):
            StructuredToolLoop({}, arguments={}, deadline=time.monotonic() + 10, **options)
    else:
        with pytest.raises(ValueError):
            loop(**options)


@pytest.mark.parametrize('kind', ['deadline', 'cancelled'])
def test_tool_boundary_exceptions_have_stable_codes(kind):
    from prosaic_runtime.events import Cancelled
    from prosaic_runtime.tools import ToolDeadlineExceeded

    def stop(args):
        raise ToolDeadlineExceeded('private') if kind == 'deadline' else Cancelled('private')
    driver = loop(stop).turns(admit=complete)
    next(driver)
    with pytest.raises(StructuredToolError, match='execution-timeout' if kind == 'deadline' else 'execution-cancelled'):
        driver.send(result({'tool': {'name': 'lookup', 'arguments': {'id': 'one'}}}))
