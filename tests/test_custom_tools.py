import json
from copy import deepcopy

import pytest
import prosaic_runtime

SCHEMA = {'type': 'object', 'required': ['sku'], 'properties': {
    'sku': {'type': 'string', 'pattern': '^SKU-[0-9]{3}$'}}, 'additionalProperties': False}


def test_legacy_descriptor_golden():
    schema = {'type': 'object', 'properties': {}, 'additionalProperties': False}
    tool = prosaic_runtime.CustomTool('lookup', 'Lookup', schema, lambda args: {}, 'v1')
    assert tool.descriptor == {
        'name': 'lookup', 'description': 'Lookup', 'parameters': schema,
        'version': 'v1', 'max_argument_bytes': 16384,
        'max_result_bytes': 65536, 'authorization_required': False}


def test_public_definition_snapshots_schema():
    assert hasattr(prosaic_runtime, 'CustomTool')
    schema = deepcopy(SCHEMA)
    tool = prosaic_runtime.CustomTool('lookup_catalog', 'Lookup', schema, lambda a: a, 'v1')
    schema['properties']['sku']['pattern'] = '.*'
    tool.parameters['properties']['sku']['pattern'] = '.*'
    assert tool.parameters['properties']['sku']['pattern'] == '^SKU-[0-9]{3}$'


def test_authorizer_cannot_mutate_handler_arguments():
    assert hasattr(prosaic_runtime, 'CustomTool')
    from prosaic_runtime.tools import execute_custom_tool
    seen = []
    def authorize(args):
        args['sku'] = 'SKU-999'
        return True
    tool = prosaic_runtime.CustomTool('lookup_catalog', 'Lookup', SCHEMA,
        lambda a: seen.append(a) or {'found': True}, 'v1', authorize=authorize)
    assert execute_custom_tool(tool, '{"sku":"SKU-001"}', check_boundary=lambda: None) == {
        'status': 'ok', 'result': {'found': True}}
    assert seen == [{'sku': 'SKU-001'}]


@pytest.mark.parametrize('change', [
    {'name': 'read_file'}, {'name': 'shell tool'}, {'name': 'é'}, {'name': 'a'*65},
    {'description': ''}, {'description': 'x'*4097}, {'handler': None},
    {'version': ''}, {'version': 'x'*129}, {'max_argument_bytes': True},
    {'max_result_bytes': 0}, {'max_argument_bytes': -1}, {'authorize': 'yes'},
    {'parameters': {'type': 'array'}}, {'parameters': {'type': 'object'}},
    {'parameters': {**SCHEMA, '$ref': '#'}},
    {'parameters': {**SCHEMA, 'properties': {'x': {'$dynamicRef': '#'}}}},
    {'parameters': {**SCHEMA, 'properties': {'x': {'type': 'invalid'}}}},
])
def test_invalid_definitions_rejected(change):
    values = dict(name='lookup_catalog', description='Lookup', parameters=SCHEMA,
                  handler=lambda a: a, version='v1') | change
    with pytest.raises(ValueError):
        prosaic_runtime.CustomTool(**values)


@pytest.mark.parametrize('raw', ['{', '{"sku":"SKU-001","sku":"SKU-002"}',
    '{"sku":NaN}', '{"sku":Infinity}', '{"sku":1e999}', '[]', 'null',
    '{}', '{"sku":1}', '{"sku":"bad"}', '{"sku":"SKU-001","extra":true}',
    '{"sku":' + '['*65 + '0' + ']'*65 + '}'])
def test_bad_arguments_never_reach_handler(raw):
    from prosaic_runtime.tools import execute_custom_tool
    seen = []
    tool = prosaic_runtime.CustomTool('lookup_catalog', 'Lookup', SCHEMA,
        lambda a: seen.append(a), 'v1')
    assert execute_custom_tool(tool, raw, check_boundary=lambda: None) == {
        'status': 'error', 'error': 'invalid_arguments'}
    assert seen == []


@pytest.mark.parametrize('decision', [False, None, 1, 'yes'])
def test_authorization_requires_true(decision):
    from prosaic_runtime.tools import execute_custom_tool
    seen = []
    tool = prosaic_runtime.CustomTool('lookup_catalog', 'Lookup', SCHEMA,
        lambda a: seen.append(a), 'v1', authorize=lambda a: decision)
    assert execute_custom_tool(tool, '{"sku":"SKU-001"}', check_boundary=lambda: None) == {
        'status': 'error', 'error': 'authorization_denied'}
    assert seen == []


def throw_secret(args):
    raise RuntimeError('synthetic-private-key')


@pytest.mark.parametrize('field,error', [('handler', 'handler_error'), ('authorize', 'authorization_denied')])
def test_exceptions_redacted(field, error):
    from prosaic_runtime.tools import execute_custom_tool
    values = dict(name='lookup_catalog', description='Lookup', parameters=SCHEMA,
                  handler=lambda a: a, version='v1') | {field: throw_secret}
    assert execute_custom_tool(prosaic_runtime.CustomTool(**values), '{"sku":"SKU-001"}',
        check_boundary=lambda: None) == {'status': 'error', 'error': error}


@pytest.mark.parametrize('value', [float('nan'), float('inf'), {1: 'x'}, object(), (1, 2)])
def test_invalid_results(value):
    from prosaic_runtime.tools import execute_custom_tool
    tool = prosaic_runtime.CustomTool('lookup_catalog', 'Lookup', SCHEMA, lambda a: value, 'v1')
    assert execute_custom_tool(tool, '{"sku":"SKU-001"}', check_boundary=lambda: None) == {
        'status': 'error', 'error': 'invalid_result'}


def test_result_depth_cycles_and_byte_bounds():
    from prosaic_runtime.tools import bounded_result
    nested = []
    for _ in range(64):
        nested = [nested]
    cycle = []; cycle.append(cycle)
    for value in (nested, cycle):
        assert bounded_result(value, 65536) == {'status': 'error', 'error': 'invalid_result'}
    assert bounded_result('large', 1) == {'status': 'error', 'error': 'result_limit'}
    assert bounded_result({'status': 'ok', 'read_receipts': ['forged']}, 65536) == {
        'status': 'ok', 'result': {'status': 'ok', 'read_receipts': ['forged']}}


def test_registration_snapshot_and_descriptor_views():
    from prosaic_runtime.tools import validate_custom_tools, custom_descriptors
    tool = prosaic_runtime.CustomTool('lookup_catalog', 'Lookup', SCHEMA, lambda a: a, 'v1')
    registry = {'lookup_catalog': tool}
    snapshot = validate_custom_tools(registry)
    registry.clear()
    view = custom_descriptors(snapshot)
    view['lookup_catalog']['version'] = 'other'
    assert custom_descriptors(snapshot)['lookup_catalog']['version'] == 'v1'
    assert 'handler' not in tool.descriptor
    with pytest.raises(ValueError):
        validate_custom_tools({'wrong_name': tool})
    with pytest.raises((AttributeError, TypeError)):
        tool.version = 'changed'


@pytest.mark.parametrize('at', [1, 2, 3])
def test_boundary_failure_is_preserved(at):
    from prosaic_runtime.tools import execute_custom_tool, ToolDeadlineExceeded
    seen, checks = [], []
    def boundary():
        checks.append(1)
        if len(checks) == at:
            raise ToolDeadlineExceeded()
    tool = prosaic_runtime.CustomTool('lookup_catalog', 'Lookup', SCHEMA,
        lambda a: seen.append(a), 'v1')
    with pytest.raises(ToolDeadlineExceeded):
        execute_custom_tool(tool, '{"sku":"SKU-001"}', check_boundary=boundary)
    assert len(seen) == (1 if at == 3 else 0)


def test_argument_byte_limit_and_cancelled_handler():
    from prosaic_runtime.tools import execute_custom_tool, bounded_result
    from prosaic_runtime.events import Cancelled
    seen = []
    tool = prosaic_runtime.CustomTool('lookup_catalog', 'Lookup', SCHEMA,
        lambda a: seen.append(a), 'v1', max_argument_bytes=1)
    assert execute_custom_tool(tool, '{"sku":"SKU-001"}', check_boundary=lambda: None)['error'] == 'invalid_arguments'
    assert seen == []
    def cancelled(args):
        raise Cancelled('cancelled')
    tool = prosaic_runtime.CustomTool('lookup_catalog', 'Lookup', SCHEMA, cancelled, 'v1')
    with pytest.raises(Cancelled):
        execute_custom_tool(tool, '{"sku":"SKU-001"}', check_boundary=lambda: None)
    value = 0
    for _ in range(64):
        value = [value]
    assert bounded_result(value, 65536)['status'] == 'ok'
