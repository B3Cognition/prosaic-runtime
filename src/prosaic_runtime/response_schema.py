"""Bounded trusted JSON Schema requests; application validation stays separate."""
import hashlib
import json
import math

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

MAX_SCHEMA_BYTES = 65536


def _check(value, depth=0, count=None):
    count = [0] if count is None else count
    count[0] += 1
    if depth > 64 or count[0] > 32768:
        raise ValueError('response_schema exceeds structural limits')
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError('response_schema needs string keys')
            if key in {'$ref', '$dynamicRef'} and (type(item) is not str or not item.startswith('#')):
                raise ValueError('response_schema cannot reference external resources')
            _check(item, depth + 1, count)
    elif type(value) is list:
        for item in value:
            _check(item, depth + 1, count)
    elif type(value) is float:
        if not math.isfinite(value):
            raise ValueError('response_schema needs finite JSON numbers')
    elif value is not None and type(value) not in (str, int, bool):
        raise ValueError('response_schema must contain only JSON values')


def schema_bytes(schema):
    return json.dumps(schema, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')


def own_response_schema(schema):
    if schema is None:
        return None
    try:
        if type(schema) is not dict or schema.get('type') != 'object':
            raise ValueError('response_schema must be an object JSON Schema')
        _check(schema)
        data = schema_bytes(schema)
        if len(data) > MAX_SCHEMA_BYTES:
            raise ValueError('response_schema exceeds byte limit')
        owned = json.loads(data)
        Draft202012Validator.check_schema(owned)
        return owned
    except (SchemaError, TypeError, UnicodeError, RecursionError) as exc:
        raise ValueError('invalid response_schema') from exc


def schema_digest(schema):
    return hashlib.sha256(schema_bytes(schema)).hexdigest()


def schema_format(schema):
    return {'type': 'json_schema', 'json_schema': {'name': 'response', 'schema': schema, 'strict': True}}
