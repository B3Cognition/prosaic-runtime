"""Bounded trusted-host accounting labels; never authorization or request data."""
from collections.abc import Mapping
import json
import re
from types import MappingProxyType


_KEY = re.compile(r'[A-Za-z][A-Za-z0-9_.-]{0,63}\Z')
_RESERVED = frozenset('''application_id tenant_id billing_account_id actor_id project_id
    request_id run_id invocation_id parent_invocation_id step_id namespace environment
    provider_id credential_account authorization api_key api-key headers request_headers
    prompt messages tools input input_data'''.split())


def snapshot_metadata(value):
    """Validate and own a read-only string map, normalizing empty maps to None."""
    if value is None:
        return None
    try:
        if not isinstance(value, Mapping) or len(value) > 16:
            raise ValueError()
        labels = {}
        for key, text in value.items():
            if len(labels) >= 16 or type(key) is not str or _KEY.fullmatch(key) is None:
                raise ValueError()
            if key.lower() in _RESERVED or key in labels:
                raise ValueError()
            if (type(text) is not str or not 1 <= len(text) <= 256 or not text.isprintable()
                    or len(text.encode('utf-8')) > 256):
                raise ValueError()
            labels[key] = text
        # Match accounting.canonical exactly, including ASCII escaping of Unicode.
        if len(json.dumps(labels, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')) > 4096:
            raise ValueError()
        return MappingProxyType(labels) if labels else None
    except Exception:
        raise ValueError('invalid request_metadata') from None


def encode_metadata(labels):
    return {'version': 1, 'labels': dict(labels)}


def decode_metadata(value):
    """A persisted extension must be present, nonempty and exactly version1."""
    if (type(value) is not dict or set(value) != {'version', 'labels'}
            or type(value['version']) is not int or value['version'] != 1
            or type(value['labels']) is not dict or not value['labels']):
        raise ValueError('invalid request_metadata schema')
    return snapshot_metadata(value['labels'])
