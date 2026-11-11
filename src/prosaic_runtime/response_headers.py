"""Bounded response-header signals, independent of token/usage evidence."""
import hashlib


def header_signals(response):
    headers = getattr(response, 'headers', None) or {}
    warning = headers.get('Warning')
    warnings = headers.get_all('Warning', []) if hasattr(headers, 'get_all') else [warning]
    # urllib already bounds HTTP header lines and their count. Inspect each
    # full value so a comma-combined warning cannot hide a later downgrade.
    normalized_warnings = (value.lower() for value in warnings if isinstance(value, str))
    unavailable = any('response_format' in value and 'not enforced' in value
                      for value in normalized_warnings)
    request_id = None
    for name in ('X-oMLX-Request-ID', 'X-Request-ID', 'OpenAI-Request-ID', 'Request-ID'):
        value = headers.get(name)
        if isinstance(value, str) and 0 < len(value.strip()) <= 256:
            request_id = value.strip()
            break
    return {
        'warning_present': warning is not None,
        'schema_enforcement': 'unavailable' if unavailable else 'not-reported',
        'server_request_id_sha256': hashlib.sha256(request_id.encode()).hexdigest() if request_id else None,
    }


def strict_schema_requested(payload):
    response_format = payload.get('response_format')
    return (isinstance(response_format, dict) and response_format.get('type') == 'json_schema'
            and isinstance(response_format.get('json_schema'), dict)
            and response_format['json_schema'].get('strict') is True)
