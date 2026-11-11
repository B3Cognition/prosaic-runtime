"""Declared schema order is opt-in, owned, bounded and evidenced at transport."""
from copy import deepcopy
from hashlib import sha256
import json

import pytest
from prosaic_runtime import RuntimeAdapter
from prosaic_runtime.response_schema import schema_digest
from test_response_schema import agent, runtime, service

SCHEMA = {'type': 'object', 'properties': {
    'questionParts': {'type': 'array', 'items': {'type': 'object', 'properties': {
        'text': {'type': 'string'}, 'claims': {'type': 'array', 'items': {'type': 'string'}}},
        'required': ['text', 'claims'], 'additionalProperties': False}},
    'claims': {'type': 'array', 'items': {'type': 'string'}}},
    'required': ['questionParts', 'claims'], 'additionalProperties': False}


def wire_digest(schema):
    return sha256(json.dumps(schema, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


@pytest.mark.parametrize('streaming', [False, True])
def test_declared_nested_order_reaches_http_and_default_stays_canonical(streaming):
    with service() as (url, requests):
        rt = runtime(url, streaming=streaming)
        ordered = rt.run(agent(), response_schema=SCHEMA, response_schema_order='declared')
        canonical = rt.run(agent(), response_schema=SCHEMA)
    assert ordered.exit_code == canonical.exit_code == 0
    actual = requests[0]['response_format']['json_schema']['schema']
    assert list(actual['properties']) == ['questionParts', 'claims']
    assert list(actual['properties']['questionParts']['items']['properties']) == ['text', 'claims']
    assert list(requests[1]['response_format']['json_schema']['schema']['properties']) == ['claims', 'questionParts']
    assert ordered.metadata['response_schema_v1'] == canonical.metadata['response_schema_v1'] == {
        'schema_sha256': schema_digest(SCHEMA), 'strict': True}
    assert ordered.metadata['response_schema_order_v1'] == {'mode': 'declared', 'wire_schema_sha256': wire_digest(actual)}
    assert 'response_schema_order_v1' not in canonical.metadata
    assert ordered.token_usage == canonical.token_usage == 7
    assert 'json_schema_order_v1' in rt.capabilities


def test_declared_adapter_inherits_owned_order_and_records_wire_identity():
    source = deepcopy(SCHEMA)
    def invoke(artifact, arguments, *, execute, **options):
        options['response_schema']['properties'].clear()
        source['properties'].clear()
        return execute(arguments)
    with service() as (url, requests):
        result = RuntimeAdapter(runtime(url), 'ordered', invoke).run(agent(),
            response_schema=source, response_schema_order='declared')
    assert result.exit_code == 0
    actual = requests[0]['response_format']['json_schema']['schema']
    assert list(actual['properties']) == ['questionParts', 'claims']
    record = result.metadata['runtime_adapter_v1']['attempts'][0]
    assert record['response_schema_sha256'] == schema_digest(SCHEMA)
    assert record['response_schema_order_v1'] == result.metadata['response_schema_order_v1']


@pytest.mark.parametrize('change', ['reorder', 'mode'])
def test_adapter_cannot_replace_declared_outer_order(change):
    changed = deepcopy(SCHEMA)
    changed['properties'] = dict(reversed(list(changed['properties'].items())))
    def invoke(artifact, arguments, *, execute, **options):
        return execute(arguments, **({'response_schema': changed} if change == 'reorder'
                                     else {'response_schema_order': 'canonical'}))
    with service() as (url, requests):
        result = RuntimeAdapter(runtime(url), 'ordered', invoke).run(agent(),
            response_schema=SCHEMA, response_schema_order='declared')
    assert result.exit_code != 0 and requests == []
    assert result.metadata['failure_reason'] == 'adapter_callback_failed'


def test_adapter_can_select_declared_schema_without_outer_constraint():
    with service() as (url, requests):
        result = RuntimeAdapter(runtime(url), 'ordered', lambda a, args, *, execute, **kw:
            execute(args, response_schema=SCHEMA, response_schema_order='declared')).run(agent())
    assert result.exit_code == 0
    assert list(requests[0]['response_format']['json_schema']['schema']['properties']) == ['questionParts', 'claims']


@pytest.mark.parametrize('order', [True, None, 'any', 1, []])
def test_invalid_order_rejected_before_dispatch(order):
    with service() as (url, requests):
        with pytest.raises(ValueError, match='response_schema_order'):
            runtime(url).run(agent(), response_schema=SCHEMA, response_schema_order=order)
        assert requests == []


def test_declared_order_requires_schema():
    with service() as (url, requests):
        with pytest.raises(ValueError, match='response_schema_order'):
            runtime(url).run(agent(), response_schema_order='declared')
        assert requests == []


@pytest.mark.parametrize('schema', [dict(type='object', const=float('nan')),
    dict(type='object', description='x' * 65536), dict(type='object', **{'$ref': 'https://example.invalid'})])
def test_declared_order_keeps_strict_admission(schema):
    with service() as (url, requests):
        with pytest.raises(ValueError, match='response_schema'):
            runtime(url).run(agent(), response_schema=schema, response_schema_order='declared')
        assert requests == []
