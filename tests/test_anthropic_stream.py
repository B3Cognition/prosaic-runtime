"""Native SSE framing must not release partial tools to the execution loop."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from prosaic_runtime import RunPolicy
from test_runtime import artifact
from test_anthropic import native_server, native_runtime, message, response, tool


def events(value=None):
    value = value or message()
    start = {**value, 'content': [], 'stop_reason': None, 'stop_sequence': None,
             'usage': {**value['usage'], 'output_tokens': 1}}
    items = [{'type': 'message_start', 'message': start}, {'type': 'ping'}]
    for index, block in enumerate(value['content']):
        opening = {**block, **({'text': ''} if block['type'] == 'text' else {'input': {}})}
        items.append({'type': 'content_block_start', 'index': index, 'content_block': opening})
        if block['type'] == 'text':
            delta = {'type': 'text_delta', 'text': block['text']}
            items.append({'type': 'content_block_delta', 'index': index, 'delta': delta})
        else:
            raw = json.dumps(block['input'])
            for partial in (raw[:5], raw[5:]):
                items.append({'type': 'content_block_delta', 'index': index,
                              'delta': {'type': 'input_json_delta', 'partial_json': partial}})
        items.append({'type': 'content_block_stop', 'index': index})
    items.extend([{'type': 'message_delta', 'delta': {'stop_reason': value['stop_reason'], 'stop_sequence': None},
                   'usage': {'output_tokens': value['usage']['output_tokens']}}, {'type': 'message_stop'}])
    return items


def sse(items, multiline=False):
    body = ''
    for item in items:
        raw = json.dumps(item, indent=2) if multiline else json.dumps(item)
        body += 'event: ' + item['type'] + '\n' + ''.join('data: ' + line + '\n' for line in raw.splitlines()) + '\n'
    return 200, 'text/event-stream', body.encode(), {}


@pytest.mark.parametrize('multiline', [False, True])
def test_native_streamed_text_is_provisional_and_usage_is_cumulative(native_server, multiline):
    url, requests, responses = native_server
    responses.append(sse(events(message('hello', usage={'input_tokens': 5, 'output_tokens': 7})), multiline))
    seen = []
    result = native_runtime(url, features={'streaming': True}).run(artifact(), on_event=seen.append)
    assert result.exit_code == 0 and result.stdout == 'hello' and result.token_usage == 12
    assert result.metadata['streamed'] is True
    assert requests[0]['body']['stream'] is True
    text = [e for e in seen if e['event'] == 'text_delta']
    assert ''.join(e['text'] for e in text) == 'hello'
    assert seen[-1]['event'] == 'completed'


def test_streamed_fragmented_tool_input_executes_after_complete_message(native_server, tmp_path):
    url, requests, responses = native_server
    (tmp_path / 'input').write_text('evidence')
    responses.extend([sse(events(message('checking', [tool(path='input')]))), sse(events())])
    seen = []
    result = native_runtime(url, features={'streaming': True}).run(artifact('read'), cwd=tmp_path,
        policy=RunPolicy(allowed_tools=frozenset({'read_file'}), read_roots=('input',), initial_tool='read_file'),
        on_event=seen.append)
    assert result.exit_code == 0 and result.token_usage == 14
    assert len([e for e in seen if e['event'] == 'tool_started']) == 1
    assert 'evidence' in requests[1]['body']['messages'][2]['content'][0]['content']
    assert requests[1]['body']['messages'][1]['content'][1]['input'] == {'path': 'input'}


@pytest.mark.parametrize('mutation', ['missing_stop', 'duplicate_stop', 'wrong_index', 'duplicate_block',
    'delta_after_close', 'nonobject', 'truncated_json', 'thinking', 'error', 'unknown', 'wrong_event_name',
    'missing_message_delta', 'early_stop'])
def test_malformed_native_stream_executes_no_tools(native_server, tmp_path, mutation):
    url, requests, responses = native_server
    items = events(message('', [tool(path='input')]))
    if mutation == 'missing_stop':
        items.pop()
    elif mutation == 'duplicate_stop':
        items.append({'type': 'message_stop'})
    elif mutation == 'wrong_index':
        items[2]['index'] = 4
    elif mutation == 'duplicate_block':
        items.insert(3, deepcopy(items[2]))
    elif mutation == 'delta_after_close':
        items.insert(-2, deepcopy(items[3]))
    elif mutation == 'nonobject':
        items[3]['delta']['partial_json'], items[4]['delta']['partial_json'] = '[', ']'
    elif mutation == 'truncated_json':
        items[4]['delta']['partial_json'] = ''
    elif mutation == 'thinking':
        items[2]['content_block']['type'] = 'thinking'
    elif mutation == 'error':
        items.insert(-1, {'type': 'error', 'error': {'type': 'overloaded_error', 'message': 'secret'}})
    elif mutation == 'unknown':
        items.insert(-1, {'type': 'new_tool_event', 'input': {}})
    elif mutation == 'missing_message_delta':
        items.pop(-2)
    elif mutation == 'early_stop':
        items.insert(3, {'type': 'message_stop'})
    value = sse(items)
    if mutation == 'wrong_event_name':
        value = (*value[:2], value[2].replace(b'event: message_start', b'event: message_stop'), value[3])
    responses.append(value)
    seen = []
    result = native_runtime(url, features={'streaming': True}).run(artifact('read'), cwd=tmp_path,
        policy=RunPolicy(allowed_tools=frozenset({'read_file'}), read_roots=('input',)), on_event=seen.append)
    assert result.exit_code != 0 and len(requests) == 1
    assert not any(e['event'] == 'tool_started' for e in seen)
    assert 'secret' not in result.stderr


@pytest.mark.parametrize('streaming', [False, True])
def test_native_invalid_utf8_returns_failure(native_server, streaming):
    url, requests, responses = native_server
    responses.append((200, 'text/event-stream' if streaming else 'application/json', b'\xff\n\n', {}))
    result = native_runtime(url, features={'streaming': streaming}).run(artifact())
    assert result.exit_code != 0 and result.metadata['provider_error_code'] == 'malformed_response'


@pytest.mark.parametrize('streaming', [False, True])
def test_native_cancelled_response_cannot_execute_tools(native_server, tmp_path, streaming):
    url, requests, responses = native_server
    value = message('provisional', [tool(path='input')])
    responses.append(sse(events(value)) if streaming else response(value))
    stopped, seen = [], []

    def on_event(event):
        seen.append(event)
        if ((streaming and event['event'] == 'text_delta') or
                (not streaming and event['event'] == 'progress' and 'response finish_reason=' in event['text'])):
            stopped.append(True)

    result = native_runtime(url, features={'streaming': streaming}).run(artifact('read'), cwd=tmp_path,
        policy=RunPolicy(allowed_tools=frozenset({'read_file'}), read_roots=('input',)),
        on_event=on_event, cancelled=lambda: bool(stopped))
    assert result.exit_code == 130 and len(requests) == 1
    assert not any(e['event'] == 'tool_started' for e in seen)


@pytest.mark.parametrize('streaming', [False, True])
def test_native_response_budget_applies_without_tools(native_server, streaming):
    url, requests, responses = native_server
    rt = native_runtime(url, features={'streaming': streaming})
    endpoint = replace(rt.config.profiles['native'], max_response_bytes=30)
    rt.config = replace(rt.config, profiles={'native': endpoint})
    responses.append(sse(events()) if streaming else response())
    result = rt.run(artifact())
    assert result.exit_code == 1 and result.metadata['failure_reason'] == 'budget_exceeded'


def test_native_request_budget_checked_before_network(native_server):
    url, requests, responses = native_server
    result = native_runtime(url).run(artifact(), policy=RunPolicy(max_input_bytes=80))
    assert result.exit_code == 1 and result.metadata['failure_reason'] == 'budget_exceeded'
    assert requests == []


@pytest.mark.parametrize('status', [307, 401, 429, 500])
def test_native_http_error_does_not_redirect_or_retry(native_server, status):
    url, requests, responses = native_server
    responses.append((status, 'application/json', b'{"error":{"message":"secret"}}', {'Location': url + '/destination'}))
    result = native_runtime(url).run(artifact(), env={'TEST_ANTHROPIC_KEY': 'fixture-key'})
    assert result.exit_code == status and result.metadata['http_status'] == status
    assert len(requests) == 1 and 'secret' not in result.stderr


def test_native_compacted_tool_results_are_not_errors(native_server, tmp_path):
    url, requests, responses = native_server
    (tmp_path / 'input').write_text('long evidence ' * 100)
    responses.extend([response(text='', calls=[tool(path='input')]), response()])
    result = native_runtime(url, features={'compact_after_tool_results': 0, 'keep_recent_tool_results': 0,
        'compact_tool_result_after_chars': 1}).run(artifact('read'), cwd=tmp_path,
            policy=RunPolicy(allowed_tools=frozenset({'read_file'}), read_roots=('input',)))
    assert result.exit_code == 0
    block = requests[1]['body']['messages'][2]['content'][0]
    assert 'compacted' in block['content'] and not block.get('is_error', False)


@pytest.mark.parametrize('streaming', [False, True])
def test_native_http_timeout_respects_invocation_budget(native_server, streaming):
    url, requests, responses = native_server
    value = sse(events()) if streaming else response()
    responses.append((*value, 0.1))
    result = native_runtime(url, features={'streaming': streaming}).run(artifact(), policy=RunPolicy(timeout_s=0.03))
    assert result.exit_code == 1 and result.timed_out
    assert result.metadata['provider_error_code'] == 'timeout'
    assert len(requests) == 1


@pytest.mark.parametrize('stop', ['max_tokens', 'refusal', 'pause_turn'])
def test_streamed_failed_stop_does_not_execute_tools(native_server, stop):
    url, requests, responses = native_server
    responses.append(sse(events(message('partial', [tool(path='input')], stop=stop))))
    seen = []
    result = native_runtime(url, features={'streaming': True}).run(artifact('read'),
        policy=RunPolicy(allowed_tools=frozenset({'read_file'})), on_event=seen.append)
    assert result.exit_code != 0 and result.metadata['raw_response_metadata']['stop_reason'] == stop
    assert not any(e['event'] == 'tool_started' for e in seen)


def test_native_json_drip_feed_cannot_extend_invocation_deadline(native_server):
    url, requests, responses = native_server
    value = response()
    responses.append((*value[:3], {'Fixture-Drip': '0.02'}))
    result = native_runtime(url).run(artifact(), policy=RunPolicy(timeout_s=0.07))
    assert result.exit_code == 1 and result.timed_out


def test_native_http_error_invalid_utf8_is_still_a_failure(native_server):
    url, requests, responses = native_server
    responses.append((429, 'application/json', b'\xff', {}))
    result = native_runtime(url).run(artifact())
    assert result.exit_code == 429 and result.metadata['http_status'] == 429
