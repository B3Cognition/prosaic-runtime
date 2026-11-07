"""Bounded host-only HTTP options, never prompt data or persistent identity."""
from collections.abc import Mapping
import re

_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}\Z")
_RESERVED = frozenset({
    'authorization', 'api-key', 'x-api-key', 'host', 'content-length', 'content-type',
    'transfer-encoding', 'connection', 'keep-alive', 'te', 'trailer', 'upgrade',
    'cookie', 'set-cookie', 'accept-encoding', 'x-correlation-id', 'x-client-request-id',
})


def snapshot_headers(value):
    """Own a tuple of validated lowercase names/ASCII values before callbacks."""
    if value is None:
        return ()
    try:
        if not isinstance(value, Mapping) or len(value) > 32:
            raise ValueError()
        result, size = {}, 0
        for name, content in value.items():
            if len(result) >= 32:
                raise ValueError()
            if type(name) is not str or _TOKEN.fullmatch(name) is None:
                raise ValueError()
            name = name.lower()
            if name in _RESERVED or name.startswith('proxy-') or name in result:
                raise ValueError()
            if type(content) is not str or len(content) > 2048 or any(not 32 <= ord(c) <= 126 for c in content):
                raise ValueError()
            size += len(name) + len(content) + 4
            if size > 8192:
                raise ValueError()
            result[name] = content
        return tuple(sorted(result.items()))
    except Exception:
        raise ValueError('invalid request_headers') from None
