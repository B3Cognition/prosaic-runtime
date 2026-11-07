"""Host invocation headers reach only real OpenAI-compatible HTTP requests."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from prosaic_runtime import ProsaicRuntime
from prosaic_runtime.accounting import MemoryRecorder
from support.conversation_server import ConversationFixture


@pytest.fixture
def wire(monkeypatch):
    monkeypatch.setenv('no_proxy', '*')
    with ConversationFixture('openai-compatible') as fx:
        headers = []
        original = fx.http.RequestHandlerClass.do_POST
        def capture(handler):
            headers.append({k.lower(): v for k, v in handler.headers.items()})
            return original(handler)
        monkeypatch.setattr(fx.http.RequestHandlerClass, 'do_POST', capture)
        yield fx, headers


@pytest.mark.parametrize('entry', ['run', 'turn', 'prepared'])
@pytest.mark.parametrize('accounted', [False, True])
def test_headers_are_host_options_not_prompt_or_receipt_data(wire, entry, accounted):
    fx, headers = wire
    recorder = MemoryRecorder() if accounted else None
    runner = ProsaicRuntime(fx.runtime.config, accounting=recorder)
    fx.queue_text('done')
    options = {'X-Tokenproxy-No-Cache': '1', 'X-Benchmark-Variant': 'private-benchmark-label'}
    agent = fx.agent
    before = agent.digest
    if entry == 'run':
        result = runner.run(agent, request_headers=options)
    else:
        prepared = runner.prepare_turn(agent, message='hello')
        identity = prepared.context_identity
        result = runner.turn(prepared if entry == 'prepared' else agent,
                             **({} if entry == 'prepared' else {'message': 'hello'}), request_headers=options)
        assert runner.prepare_turn(agent, message='hello').context_identity == identity
        result = result.result
    assert result.exit_code == 0
    assert agent.digest == before and headers[0]['x-tokenproxy-no-cache'] == '1'
    assert headers[0]['x-benchmark-variant'] == 'private-benchmark-label'
    assert 'private-benchmark-label' not in json.dumps(fx.requests) + json.dumps(result.metadata)
    if recorder:
        assert 'private-benchmark-label' not in json.dumps(recorder.intents) + json.dumps(recorder.observations)
        assert headers[0]['x-correlation-id'] in recorder.intents


@pytest.mark.parametrize('entry', ['run', 'turn'])
def test_snapshot_precedes_callbacks_and_does_not_affect_next_invocation(wire, entry):
    fx, headers = wire
    runner = fx.runtime
    supplied = {'x-tokenproxy-no-cache': '1'}
    def observer(event):
        if event['event'] == 'invocation_started':
            supplied['x-tokenproxy-no-cache'] = '0'
    fx.queue_text('one'); fx.queue_text('two')
    if entry == 'run':
        runner.run(fx.agent, request_headers=supplied, observer=observer)
        runner.run(fx.agent)
    else:
        runner.turn(fx.agent, message='one', request_headers=supplied, observer=observer)
        runner.turn(fx.agent, message='two')
    assert headers[0]['x-tokenproxy-no-cache'] == '1'
    assert 'x-tokenproxy-no-cache' not in headers[1]


def test_tool_continuations_keep_the_same_private_header_snapshot(wire):
    fx, headers = wire
    supplied = {'x-tokenproxy-no-cache': '1'}
    fx.add_tool('lookup', lambda args: supplied.update({'x-tokenproxy-no-cache': '0'}) or {'ok': True})
    fx.queue_calls([{'id': 'one', 'name': 'lookup', 'arguments': '{}'}]); fx.queue_text('done')
    result = fx.runtime.turn(fx.agent, message='lookup', policy=fx.policy, request_headers=supplied)
    assert result.result.exit_code == 0 and len(headers) == 2
    assert [h['x-tokenproxy-no-cache'] for h in headers] == ['1', '1']


@pytest.mark.parametrize('options', [
    [], {'Bad Name': '1'}, {'X-Test': 'line\r\ninjected: true'}, {'X-Test': '\x00'},
    {'X-Test': 'é'}, {'X-Test': 1}, {'X-Test': 'x' * 2049},
    {'X-Test': '1', 'x-test': '2'}, {f'X-{i}': 'v' for i in range(33)},
    {f'X-{i}': 'v' * 1024 for i in range(9)},
    *[{name: 'do-not-echo'} for name in ['Authorization', 'host', 'CONTENT-LENGTH', 'Content-Type',
      'Proxy-Authorization', 'Connection', 'Transfer-Encoding', 'X-Correlation-ID', 'X-Client-Request-Id', 'X-Api-Key']],
])
@pytest.mark.parametrize('entry', ['run', 'turn'])
def test_invalid_headers_refuse_before_callbacks_or_provider(wire, options, entry):
    fx, headers = wire
    observed = []
    with pytest.raises(ValueError) as exc:
        if entry == 'run': fx.runtime.run(fx.agent, request_headers=options, observer=observed.append)
        else: fx.runtime.turn(fx.agent, message='hello', request_headers=options, observer=observed.append)
    assert 'do-not-echo' not in str(exc.value)
    assert headers == fx.requests == observed == []


def test_nonempty_headers_refuse_on_anthropic_before_any_request():
    with ConversationFixture('anthropic') as fx:
        with pytest.raises(ValueError):
            fx.runtime.turn(fx.agent, message='hello', request_headers={'x-tokenproxy-no-cache': '1'})
        assert fx.requests == []


def test_empty_headers_preserve_admission_and_payload(wire):
    fx, headers = wire
    for options in [None, {}]:
        fx.queue_text('done')
        assert fx.runtime.run(fx.agent, request_headers=options).exit_code == 0
    assert fx.requests[0] == fx.requests[1]


def test_concurrent_calls_on_one_runtime_keep_separate_options(wire):
    from threading import Barrier
    fx, headers = wire
    runner, barrier = fx.runtime, Barrier(2)
    fx.queue_text('one'); fx.queue_text('two')
    def invoke(value):
        return runner.run(fx.agent, request_headers={'x-benchmark-variant': value},
                          observer=lambda event: barrier.wait(timeout=5) if event['event'] == 'invocation_started' else None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, ['one', 'two']))
    assert all(result.exit_code == 0 for result in results)
    assert sorted(h['x-benchmark-variant'] for h in headers) == ['one', 'two']


def test_rule_retry_keeps_headers_without_adding_them_to_feedback(wire):
    from prosaic_runtime import ProsaicArtifact, generic_guards
    fx, headers = wire
    agent = ProsaicArtifact('rules', 'subagent', {'tools': []},
        '- ALWAYS answer JSON. <!-- enforce: strict_json(stage=final, on_violation=retry) -->\n')
    runner = ProsaicRuntime(fx.runtime.config, guards=generic_guards())
    fx.queue_text('not JSON'); fx.queue_text('{}')
    result = runner.turn(agent, message='hello', request_headers={'x-tokenproxy-no-cache': '1'})
    assert result.result.exit_code == 0 and len(headers) == 2
    assert [h['x-tokenproxy-no-cache'] for h in headers] == ['1', '1']
    assert 'x-tokenproxy-no-cache' not in json.dumps(fx.requests)


def test_redirect_does_not_forward_private_headers(wire, monkeypatch):
    fx, headers = wire
    with ConversationFixture('openai-compatible') as other:
        def redirect(handler):
            handler.rfile.read(int(handler.headers['Content-Length']))
            handler.send_response(302)
            handler.send_header('Location', other.runtime.config.profiles['local'].base_url + '/chat/completions')
            handler.send_header('Content-Length', '0')
            handler.end_headers()
        monkeypatch.setattr(fx.http.RequestHandlerClass, 'do_POST', redirect)
        result = fx.runtime.run(fx.agent, request_headers={'x-private': 'must-not-travel'})
        assert result.exit_code != 0 and other.requests == []


def test_unsupported_provider_run_never_marks_execution_started():
    with ConversationFixture('anthropic') as fx:
        events, telemetry = [], []
        with pytest.raises(ValueError):
            fx.runtime.run(fx.agent, request_headers={'x-tokenproxy-no-cache': '1'},
                           on_event=events.append, observer=telemetry.append)
        assert fx.requests == [] and events == []
        assert telemetry[-1]['outcome'] == 'admission_failure'


def test_mapping_cannot_hide_an_extra_entry_by_underreporting_its_length(wire):
    from collections.abc import Mapping
    class GrowingMapping(Mapping):
        def __len__(self): return 32
        def __iter__(self): return iter(f'x-{i}' for i in range(33))
        def __getitem__(self, key): return 'v'
    fx, headers = wire
    fx.queue_text('done')
    with pytest.raises(ValueError):
        fx.runtime.run(fx.agent, request_headers=GrowingMapping())
    assert fx.requests == headers == []


def test_unsupported_prepared_turn_refuses_before_admission_marker_or_callbacks():
    with ConversationFixture('anthropic') as fx:
        runner = fx.runtime
        prepared = runner.prepare_turn(fx.agent, message='hello')
        markers, events, telemetry = [], [], []
        with pytest.raises(ValueError):
            runner.turn(prepared, request_headers={'x-test': 'v'}, on_admitted=lambda: markers.append(True),
                        on_event=events.append, observer=telemetry.append)
        assert fx.requests == markers == events == telemetry == []
