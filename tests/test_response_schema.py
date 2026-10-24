"""Trusted schema requests constrain generation without repairing model output."""
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread

import pytest
from prosaic_runtime import EndpointConfig, ProsaicArtifact, ProsaicRuntime, RunPolicy, RuntimeAdapter, RuntimeConfig

SCHEMA = {'type': 'object', 'properties': {'addresses': {'type': 'array', 'items': {'type': 'integer'}}},
          'required': ['addresses'], 'additionalProperties': False}


@contextmanager
def service(*, status=200, raw='{"addresses":[]}'):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            value = {'choices': [{'message': {'content': raw}, 'finish_reason': 'stop'}],
                     'usage': {'prompt_tokens': 5, 'completion_tokens': 2, 'total_tokens': 7}}
            data = json.dumps(value if status == 200 else {'error': 'unsupported schema'}).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers(); self.wfile.write(data)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try: yield f'http://127.0.0.1:{server.server_port}/v1', requests
    finally:
        server.shutdown(); server.server_close(); thread.join(5)
        assert not thread.is_alive()


def runtime(url, *, streaming=False, json_mode=False, provider='openai-compatible'):
    endpoint = EndpointConfig(url, 'fixture', provider=provider,
                              features={'streaming': streaming, 'json_mode': json_mode})
    return ProsaicRuntime(RuntimeConfig({'fixture': endpoint}, {}, 'fixture'))


def agent(**frontmatter):
    return ProsaicArtifact('schema-fixture', 'subagent', {'tools': [], **frontmatter}, 'Return JSON. {{args}}')


def expected(schema):
    return {'type': 'json_schema', 'json_schema': {'name': 'response', 'schema': schema, 'strict': True}}


@pytest.mark.parametrize('streaming', [False, True])
def test_exact_schema_reaches_real_transport_with_usage(streaming):
    with service() as (url, requests):
        result = runtime(url, streaming=streaming, json_mode=True).run(agent(), response_schema=SCHEMA,
            policy=RunPolicy(max_provider_requests=1, max_tool_calls=0, max_reported_tokens=10))
    assert result.exit_code == 0 and result.stdout == '{"addresses":[]}'
    assert result.token_usage == 7 and result.metadata['invocation_budgets_v1']['provider_requests'] == 1
    assert requests[0]['response_format'] == expected(SCHEMA)
    assert result.metadata['response_schema_v1']['schema_sha256'] == sha256(
        json.dumps(SCHEMA, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    assert result.metadata['response_schema_v1']['strict'] is True


def test_schema_is_per_invocation_and_legacy_json_mode_is_preserved():
    with service() as (url, requests):
        rt = runtime(url)
        rt.run(agent(), response_schema=SCHEMA)
        rt.run(agent())
        runtime(url, json_mode=True).run(agent())
    assert requests[0]['response_format'] == expected(SCHEMA)
    assert 'response_format' not in requests[1]
    assert requests[2]['response_format'] == {'type': 'json_object'}


@pytest.mark.parametrize('raw', ['{}', '```json\n{"addresses":[]}\n```', '{"addresses":[999]}'])
def test_provider_output_is_not_repaired_or_mistaken_for_semantic_validation(raw):
    with service(raw=raw) as (url, requests):
        result = runtime(url).run(agent(), response_schema=SCHEMA)
    assert result.stdout == raw and result.token_usage == 7 and len(requests) == 1


def test_schema_http_rejection_has_no_unconstrained_fallback():
    with service(status=400) as (url, requests):
        result = runtime(url).run(agent(), response_schema=SCHEMA,
                                 policy=RunPolicy(max_provider_requests=2))
    assert result.exit_code == 400 and len(requests) == 1
    assert requests[0]['response_format'] == expected(SCHEMA)
    assert result.metadata['invocation_budgets_v1']['provider_requests'] == 1


@pytest.mark.parametrize('schema', [True, [], {}, {'type': 'nonsense'},
    {'type': 'object', '$ref': 'https://example.invalid/schema'},
    {'type': 'object', 'const': float('nan')}, {'type': 'object', 'description': 'x' * 65536}])
def test_malformed_or_remote_or_oversized_schema_rejected_before_dispatch(schema):
    with service() as (url, requests):
        with pytest.raises(ValueError, match='response_schema'):
            runtime(url).run(agent(), response_schema=schema)
        assert requests == []


@pytest.mark.parametrize('combination', ['anthropic', 'tools', 'acquisition'])
def test_unsupported_combinations_reject_before_dispatch(combination):
    with service() as (url, requests):
        rt = runtime(url, provider='anthropic' if combination == 'anthropic' else 'openai-compatible')
        prose = agent(tools=['read_file']) if combination == 'tools' else agent()
        options = {'acquisition': agent()} if combination == 'acquisition' else {}
        with pytest.raises(ValueError, match='response_schema'):
            rt.run(prose, response_schema=SCHEMA, **options)
        assert requests == []


def test_schema_bytes_share_native_input_limit():
    schema = {**SCHEMA, 'description': 'x' * 2000}
    with service() as (url, requests):
        result = runtime(url).run(agent(), response_schema=schema, policy=RunPolicy(max_input_bytes=1000))
        assert result.exit_code != 0 and result.metadata['failure_reason'] == 'budget_exceeded'
        assert 'max_input_bytes' in result.stderr
        assert requests == []


def test_adapter_projects_schema_and_retains_one_attempt():
    with service() as (url, requests):
        adapter = RuntimeAdapter(runtime(url), 'schema-test',
            lambda artifact, arguments, *, execute, **kw: execute(arguments, response_schema=SCHEMA), max_attempts=1)
        result = adapter.run(agent(), 'input', policy=RunPolicy(max_provider_requests=1, max_tool_calls=0))
    assert 'json_schema_v1' in adapter.capabilities
    assert result.exit_code == 0 and result.token_usage == 7
    assert requests[0]['response_format'] == expected(SCHEMA)
    attempts = result.metadata['runtime_adapter_v1']['attempts']
    assert len(attempts) == 1 and attempts[0]['provider_requests'] == 1
    assert attempts[0]['response_schema_sha256'] == result.metadata['response_schema_v1']['schema_sha256']


def test_adapter_cannot_replace_outer_schema_constraint():
    with service() as (url, requests):
        adapter = RuntimeAdapter(runtime(url), 'schema-test',
            lambda artifact, arguments, *, execute, **kw: execute(arguments, response_schema={**SCHEMA, 'required': []}))
        result = adapter.run(agent(), response_schema=SCHEMA)
    assert result.exit_code != 0 and requests == []
    assert result.metadata['failure_reason'] == 'adapter_callback_failed'


@pytest.mark.parametrize('keyword', ['const', 'enum'])
def test_adapter_distinguishes_boolean_from_numeric_schema_constants(keyword):
    numeric = {'type': 'object', 'properties': {'x': {keyword: 1 if keyword == 'const' else [1]}}}
    boolean = {'type': 'object', 'properties': {'x': {keyword: True if keyword == 'const' else [True]}}}
    with service() as (url, requests):
        adapter = RuntimeAdapter(runtime(url), 'schema-test',
            lambda artifact, arguments, *, execute, **kw: execute(arguments, response_schema=boolean))
        result = adapter.run(agent(), response_schema=numeric)
    assert result.exit_code != 0 and requests == []
    assert result.metadata['failure_reason'] == 'adapter_callback_failed'


def test_adapter_callback_cannot_mutate_inherited_outer_schema():
    def invoke(artifact, arguments, *, execute, **options):
        options['response_schema']['required'].clear()
        return execute(arguments)
    with service() as (url, requests):
        result = RuntimeAdapter(runtime(url), 'schema-test', invoke).run(agent(), response_schema=SCHEMA)
    assert result.exit_code == 0 and requests[0]['response_format'] == expected(SCHEMA)


def test_adapter_inherits_outer_schema_when_hook_omits_it():
    with service() as (url, requests):
        adapter = RuntimeAdapter(runtime(url), 'schema-test',
            lambda artifact, arguments, *, execute, **kw: execute(arguments))
        result = adapter.run(agent(), response_schema=SCHEMA)
    assert result.exit_code == 0 and requests[0]['response_format'] == expected(SCHEMA)


def test_adapter_zero_request_grant_still_blocks_schema_dispatch():
    with service() as (url, requests):
        adapter = RuntimeAdapter(runtime(url), 'schema-test',
            lambda artifact, arguments, *, execute, **kw: execute(arguments, response_schema=SCHEMA))
        result = adapter.run(agent(), policy=RunPolicy(max_provider_requests=0))
    assert result.exit_code != 0 and requests == []
    assert result.metadata['failure_reason'] == 'provider_request_limit'


def test_schema_is_owned_before_observer_callbacks():
    schema = deepcopy(SCHEMA)
    def mutate(event): schema['required'].clear()
    with service() as (url, requests):
        result = runtime(url).run(agent(), response_schema=schema, observer=mutate)
    assert result.exit_code == 0 and requests[0]['response_format'] == expected(SCHEMA)
