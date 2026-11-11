"""Explicit schema downgrades cannot be accepted; timeout headers stay bounded."""
import hashlib
import json
import time
from types import SimpleNamespace

import pytest
from test_transport_diagnostics import BODY, run_wire, trace

DOWNGRADE = '199 omlx "response_format strict json_schema not enforced; grammar-constrained decoding unavailable, output is best-effort and NOT schema-enforced"'
SCHEMA = {'type': 'object', 'properties': {'addresses': {'type': 'array', 'items': {'type': 'integer'}}}, 'required': ['addresses'], 'additionalProperties': False}
VALID = BODY.replace(b'"content":"done"', b'"content":"{\\"addresses\\":[]}"')


def test_body_timeout_preserves_header_signals_and_private_request_id():
    result, events, rec, requests = run_wire('stalled', timeout=.25, warning=DOWNGRADE, request_id='req-local-001')
    assert result.timed_out and result.token_usage is None and len(requests) == 1
    assert next(iter(rec.observations.values()))['usage']['status'] == 'unknown'
    assert result.metadata['raw_response_headers']['x-request-id'] == 'req-local-001'
    signals = result.metadata['response_header_signals_v1']
    assert signals['schema_enforcement'] == 'unavailable' and signals['warning_present'] is True
    observed = trace(events)
    assert observed['server_request_id_sha256'] == hashlib.sha256(b'req-local-001').hexdigest()
    assert observed['schema_enforcement'] == 'unavailable'
    assert 'req-local-001' not in json.dumps(events) and DOWNGRADE not in json.dumps(events)


@pytest.mark.parametrize('enabled', [False, True])
def test_strict_schema_downgrade_fails_closed_without_consuming_answer_or_retry(enabled):
    result, events, rec, requests = run_wire(body=VALID, warning=DOWNGRADE, schema=SCHEMA, enabled=enabled)
    assert result.exit_code != 0 and result.stdout == '' and len(requests) == 1
    assert result.metadata['provider_error_code'] == 'schema_enforcement_unavailable'
    assert result.token_usage is None
    assert next(iter(rec.observations.values()))['usage']['status'] == 'unknown'
    if enabled:
        assert trace(events)['body_bytes'] == 0 and trace(events)['eof'] is False


@pytest.mark.parametrize('warning', [None, '199 proxy "unrelated sk-private-warning"'])
def test_supported_strict_output_and_unrelated_warnings_preserve_response(warning):
    result, events, rec, requests = run_wire(body=VALID, warning=warning, schema=SCHEMA)
    assert result.exit_code == 0 and result.stdout == '{"addresses":[]}' and result.token_usage == 7
    assert len(requests) == 1 and next(iter(rec.observations.values()))['usage']['total_tokens'] == 7
    observed = trace(events)
    assert observed['schema_enforcement'] == 'not-reported'
    assert observed['warning_present'] is (warning is not None)
    assert 'sk-private-warning' not in json.dumps(events)


def test_oversized_request_id_is_not_preserved_or_hashed():
    result, events, _, _ = run_wire('stalled', timeout=.25, request_id='x' * 257)
    assert result.timed_out
    assert 'x-request-id' not in result.metadata.get('raw_response_headers', {})
    assert trace(events)['server_request_id_sha256'] is None


def test_http_error_body_timeout_preserves_header_context():
    result, events, rec, requests = run_wire('stalled', timeout=.25, status=400, request_id='req-error-001')
    assert result.timed_out and result.token_usage is None and len(requests) == 1
    assert result.metadata['http_status'] == 400
    assert result.metadata['raw_response_headers']['x-request-id'] == 'req-error-001'
    assert next(iter(rec.observations.values()))['usage']['status'] == 'unknown'


def test_missing_response_headers_produces_neutral_signals():
    from prosaic_runtime.response_headers import header_signals
    assert header_signals(SimpleNamespace(headers=None)) == {
        'warning_present': False, 'schema_enforcement': 'not-reported', 'server_request_id_sha256': None}


def test_keepalive_bytes_do_not_extend_the_absolute_deadline():
    start = time.monotonic()
    result, events, rec, requests = run_wire('keepalive', timeout=.25)
    assert result.timed_out and time.monotonic() - start < 1.5
    assert result.token_usage is None and len(requests) == 1
    assert next(iter(rec.observations.values()))['usage']['status'] == 'unknown'
    observed = trace(events)
    assert observed['body_bytes'] >= 2 and observed['eof'] is False


def test_streaming_strict_downgrade_is_rejected_before_any_event_is_consumed():
    result, events, rec, requests = run_wire('sse', warning=DOWNGRADE, schema=SCHEMA, streaming=True)
    assert result.exit_code != 0 and result.stdout == '' and len(requests) == 1
    assert result.metadata['provider_error_code'] == 'schema_enforcement_unavailable'
    assert result.token_usage is None and trace(events)['body_bytes'] == 0


def test_omlx_server_request_id_is_preserved_through_timeout():
    result, events, _, requests = run_wire('stalled', timeout=.25, request_id='fixture-omlx-id', request_id_header='X-oMLX-Request-ID')
    assert result.timed_out and len(requests) == 1
    assert result.metadata['raw_response_headers']['x-omlx-request-id'] == 'fixture-omlx-id'
    assert trace(events)['server_request_id_sha256'] == hashlib.sha256(b'fixture-omlx-id').hexdigest()


def test_combined_warning_checks_for_downgrade_after_unrelated_warning():
    warning = '199 proxy "' + 'x' * 4096 + '", ' + DOWNGRADE
    result, events, _, requests = run_wire(body=VALID, warning=warning, schema=SCHEMA)
    assert len(requests) == 1 and result.stdout == ''
    assert result.metadata['provider_error_code'] == 'schema_enforcement_unavailable'
    assert trace(events)['schema_enforcement'] == 'unavailable'


def test_http_error_completion_retains_normalized_headers():
    result, events, _, requests = run_wire(body=VALID, status=400, warning=DOWNGRADE, request_id='req-error-004')
    assert len(requests) == 1 and result.exit_code != 0
    assert result.metadata['response_header_signals_v1']['schema_enforcement'] == 'unavailable'
    observed = trace(events)
    assert observed['headers_ms'] is not None and observed['exception_stage'] is None
    assert observed['server_request_id_sha256'] == hashlib.sha256(b'req-error-004').hexdigest()
