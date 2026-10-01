"""Trusted fixed-data tool registration. No model-controlled paths or imports."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from prosaic_runtime import CustomTool

SCHEMA = {'type': 'object', 'required': ['sku'], 'properties': {
    'sku': {'type': 'string', 'pattern': '^SKU-[0-9]{3}$'}}, 'additionalProperties': False}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate catalogue key')
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError('nonfinite catalogue value')


def load_catalog():
    data = Path(__file__).with_name('catalog.json').read_bytes()
    catalog = json.loads(data, object_pairs_hook=_unique, parse_constant=_nonfinite)
    json.dumps(catalog, allow_nan=False)
    return catalog, hashlib.sha256(data).hexdigest()


def make_tools():
    catalog, checksum = load_catalog()
    def lookup(args):
        item = catalog.get(args['sku'])
        return {'found': item is not None, 'item': deepcopy(item)}
    return {'lookup_catalog': CustomTool('lookup_catalog', 'Look up a synthetic catalogue item.',
                                         SCHEMA, lookup, 'catalog-' + checksum)}
