"""Native accounting evidence is independent of response admission."""
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from prosaic_runtime import RunPolicy
from prosaic_runtime.accounting import MemoryRecorder, RateCard
from test_runtime import artifact
from test_anthropic import native_server, native_runtime, message, response, tool
from test_anthropic_stream import events, sse


def observation(recorder):
    assert len(recorder.observations) == 1
    return next(iter(recorder.observations.values()))


def test_cache_creation_cannot_be_priced_as_ordinary_input():
    from prosaic_runtime.anthropic_usage import normalize_anthropic_usage
    usage = normalize_anthropic_usage({'input_tokens': 10, 'output_tokens': 4,
        'cache_read_input_tokens': 6, 'cache_creation_input_tokens': 8})
    assert usage['input_tokens'] == 24 and usage['total_tokens'] == 28
    assert usage['cached_input_tokens'] == 6 and usage['status'] == 'unsupported'
    assert RateCard('v1', 'claude-test', '2', '8', '0.5').assess({
        'response_model': 'claude-test', 'usage': usage})['amount'] is None


@pytest.mark.parametrize('streaming', [False, True])
def test_native_accounting_captures_actual_model_id_and_usage(native_server, streaming):
    url, requests, responses = native_server
    value = message('private answer', usage={'input_tokens': 10, 'output_tokens': 7,
        'cache_read_input_tokens': 6, 'cache_creation_input_tokens': 0,
        'cache_creation': {'ephemeral_5m_input_tokens': 0, 'ephemeral_1h_input_tokens': 0},
        'server_tool_use': {'web_search_requests': 0}, 'service_tier': 'standard'})
    responses.append(sse(events(value)) if streaming else response(value))
    recorder = MemoryRecorder(provider_id='anthropic')
    result = native_runtime(url, features={'streaming': streaming}, accounting=recorder).run(
        artifact(), 'private prompt')
    assert result.exit_code == 0 and result.token_usage == 23
    obs = observation(recorder)
    assert obs['usage'] == {'input_tokens': 16, 'output_tokens': 7, 'total_tokens': 23,
        'cached_input_tokens': 6, 'reasoning_output_tokens': None, 'status': 'reported'}
    assert obs['response_model'] == 'claude-test' and obs['provider_request_id'] == 'msg_test'
    assert obs['outcome'] == 'completed'
    assert next(iter(recorder.intents.values()))['provider_id'] == 'anthropic'
    assert 'private' not in json.dumps([recorder.intents, recorder.observations])
    assert RateCard('v1', 'claude-test', '2', '8', '0.5').assess(obs)['amount'] == '0.000079000000000000'


@pytest.mark.parametrize('usage,status,total', [
    ({}, 'unknown', None), ({'input_tokens': 5}, 'unknown', None),
    ({'input_tokens': True, 'output_tokens': 2}, 'untrusted', None),
    ({'input_tokens': -1, 'output_tokens': 2}, 'untrusted', None),
    ({'input_tokens': 2**63 - 1, 'output_tokens': 2}, 'untrusted', None),
    ({'input_tokens': 5, 'output_tokens': 2, 'cache_read_input_tokens': -1}, 'untrusted', None),
    ({'input_tokens': 5, 'output_tokens': 2, 'cache_creation_input_tokens': 8}, 'unsupported', 15),
    ({'input_tokens': 5, 'output_tokens': 2, 'server_tool_use': {'web_search_requests': 1}}, 'unsupported', 7),
    ({'input_tokens': 5, 'output_tokens': 2, 'new_billable_tokens': 1}, 'unsupported', 7),
    ({'input_tokens': 5, 'output_tokens': 2, 'inference_geo': 'us'}, 'unsupported', 7),
    ({'input_tokens': 5, 'output_tokens': 2, 'service_tier': 'priority'}, 'unsupported', 7),
    ({'input_tokens': 0, 'output_tokens': 0}, 'reported', 0),
])
def test_native_usage_integrity_and_unpriceable_categories(native_server, usage, status, total):
    url, requests, responses = native_server
    responses.append(response(message(usage=usage)))
    recorder = MemoryRecorder(provider_id='anthropic')
    result = native_runtime(url, accounting=recorder).run(artifact())
    assert result.exit_code == 0 and result.token_usage == total
    obs = observation(recorder)
    assert obs['usage']['status'] == status
    if status != 'reported':
        assert RateCard('v1', 'claude-test', '2', '8').assess(obs)['amount'] is None


def test_native_partial_stream_retains_partial_evidence(native_server):
    url, requests, responses = native_server
    responses.append(sse(events()[:-1]))
    recorder = MemoryRecorder(provider_id='anthropic')
    result = native_runtime(url, features={'streaming': True}, accounting=recorder).run(artifact())
    assert result.exit_code != 0 and result.token_usage is None
    obs = observation(recorder)
    assert obs['usage']['status'] == 'partial' and obs['usage']['total_tokens'] == 7
    assert obs['outcome'] == 'failed'


def test_native_usage_captured_before_cancel_callback(native_server):
    url, requests, responses = native_server
    responses.append(sse(events()))
    recorder = MemoryRecorder(provider_id='anthropic')
    stopped = []
    result = native_runtime(url, features={'streaming': True}, accounting=recorder).run(artifact(),
        on_event=lambda e: stopped.append(True) if e['event'] == 'text_delta' else None,
        cancelled=lambda: bool(stopped))
    assert result.exit_code == 130
    obs = observation(recorder)
    assert obs['usage']['input_tokens'] == 5 and obs['usage']['output_tokens'] == 1
    assert obs['usage']['status'] == 'partial' and obs['outcome'] == 'cancelled'


@pytest.mark.parametrize('mutation', ['decreasing_usage', 'duplicate_start', 'duplicate_usage_key'])
def test_native_conflicting_evidence_cannot_be_priced(native_server, mutation):
    url, requests, responses = native_server
    items = events()
    if mutation == 'decreasing_usage':
        items.insert(-1, {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 0}})
    elif mutation == 'duplicate_start':
        items.insert(1, {'type': 'message_start', 'message': {**items[0]['message'], 'id': 'different'}})
    value = sse(items)
    if mutation == 'duplicate_usage_key':
        value = (*value[:2], value[2].replace(b'"input_tokens": 5', b'"input_tokens": 5, "input_tokens": 1'), value[3])
    responses.append(value)
    recorder = MemoryRecorder(provider_id='anthropic')
    result = native_runtime(url, features={'streaming': True}, accounting=recorder).run(artifact())
    obs = observation(recorder)
    assert obs['usage']['status'] == 'untrusted'
    assert result.token_usage is None
    assert RateCard('v1', 'claude-test', '2', '8').assess(obs)['amount'] is None


def test_native_recorder_mismatch_fails_before_inference(native_server):
    url, requests, responses = native_server
    recorder = MemoryRecorder()
    with pytest.raises(ValueError, match='provider'):
        native_runtime(url, accounting=recorder).run(artifact())
    assert not requests and not recorder.intents


def test_native_failed_final_turn_does_not_erase_its_usage(native_server, tmp_path):
    url, requests, responses = native_server
    (tmp_path / 'input').write_text('evidence')
    responses.extend([response(text='', calls=[tool(path='input')]), response(stop='max_tokens')])
    recorder = MemoryRecorder(provider_id='anthropic')
    result = native_runtime(url, accounting=recorder).run(artifact('read'), cwd=tmp_path,
        policy=RunPolicy(allowed_tools=frozenset({'read_file'}), read_roots=('input',)))
    assert result.exit_code == 1 and result.token_usage == 14
    assert len(recorder.observations) == 2
    assert all(obs['usage']['total_tokens'] == 7 for obs in recorder.observations.values())


def test_native_unknown_turn_does_not_become_known_loop_total(native_server, tmp_path):
    url, requests, responses = native_server
    (tmp_path / 'input').write_text('evidence')
    first = message('', [tool(path='input')])
    first.pop('usage')
    responses.extend([response(first), response()])
    result = native_runtime(url).run(artifact('read'), cwd=tmp_path,
        policy=RunPolicy(allowed_tools=frozenset({'read_file'}), read_roots=('input',)))
    assert result.exit_code == 0 and result.token_usage is None
    assert result.metadata['reported_token_usage'] == 7


def test_native_unknown_usage_category_cannot_disappear_in_final_snapshot(native_server):
    url, requests, responses = native_server
    items = events(message(usage={'input_tokens': 5, 'output_tokens': 2, 'server_tool_use': {'web_search_requests': 1}}))
    items[-2]['usage']['server_tool_use'] = {'web_search_requests': 0}
    responses.append(sse(items))
    recorder = MemoryRecorder(provider_id='anthropic')
    result = native_runtime(url, features={'streaming': True}, accounting=recorder).run(artifact())
    assert result.token_usage is None
    assert observation(recorder)['usage']['status'] == 'untrusted'


def test_native_cli_can_select_matching_accounting_provider(native_server, tmp_path, monkeypatch, capsys):
    from prosaic_runtime.cli import main
    url, requests, responses = native_server
    responses.append(response())
    recorder = MemoryRecorder(provider_id='anthropic')

    def external_recorder(dsn, **kwargs):
        assert dsn == 'fixture-dsn'
        recorder.provider_id = kwargs.get('provider_id', 'openai-compatible')
        return recorder

    monkeypatch.setitem(sys.modules, 'prosaic_runtime_postgres', SimpleNamespace(PostgresRecorder=external_recorder))
    monkeypatch.setenv('TEST_ACCOUNTING_DSN', 'fixture-dsn')
    monkeypatch.setenv('PATH', str(Path(sys.executable).parent) + os.pathsep + os.environ.get('PATH', ''))
    config = tmp_path / 'native.yml'
    config.write_text(f'default_profile: native\nroutes: {{fast: native}}\nprofiles:\n  native:\n'
                      f'    provider: anthropic\n    base_url: {url}\n    model: claude-test\n'
                      '    features: {streaming: false}\n')
    source = tmp_path / '.prosaic'
    (source / 'subagents').mkdir(parents=True)
    (source / 'subagents/summarizer.md').write_text(
        '---\nname: summarizer\ndescription: Summarize input\nexecution: agent\n'
        'model_tier: fast\ntools: none\n---\nSummarize the input.')
    code = main(['subagents/summarizer.md', '--config', str(config), '--source', str(source),
                 '--accounting-dsn-env', 'TEST_ACCOUNTING_DSN', '--accounting-namespace', 'test',
                 '--accounting-environment', 'test', '--accounting-provider', 'anthropic', '--quiet'])
    assert code == 0 and recorder.provider_id == 'anthropic'
    assert observation(recorder)['usage']['total_tokens'] == 7
    assert json.loads(capsys.readouterr().out)['exit_code'] == 0


@pytest.mark.parametrize('mutation', ['leading_space', 'invalid_utf8', 'openai_done'])
def test_native_accounting_does_not_trust_frames_rejected_by_parser(native_server, mutation):
    url, requests, responses = native_server
    value = sse(events())
    body = value[2]
    if mutation == 'leading_space':
        body = body.replace(b'data:', b' data:')
    elif mutation == 'invalid_utf8':
        body = body.replace(b'"text": "done"', b'"text": "\xff"')
    else:
        body += b'data: [DONE]\n\n'
    responses.append((*value[:2], body, value[3]))
    recorder = MemoryRecorder(provider_id='anthropic')
    result = native_runtime(url, features={'streaming': True}, accounting=recorder).run(artifact())
    assert result.exit_code != 0
    assert observation(recorder)['usage']['status'] != 'reported'


def test_native_omitted_nested_usage_preserves_billable_evidence(native_server):
    url, requests, responses = native_server
    items = events(message(usage={'input_tokens': 5, 'output_tokens': 2,
                                  'server_tool_use': {'web_search_requests': 1}}))
    items[-2]['usage']['server_tool_use'] = {}
    responses.append(sse(items))
    recorder = MemoryRecorder(provider_id='anthropic')
    result = native_runtime(url, features={'streaming': True}, accounting=recorder).run(artifact())
    assert result.exit_code == 0 and result.token_usage == 7
    obs = observation(recorder)
    assert obs['usage']['status'] == 'unsupported'
    assert result.metadata['raw_response_metadata']['usage']['server_tool_use'] == {'web_search_requests': 1}
    assert RateCard('v1', 'claude-test', '2', '8').assess(obs)['amount'] is None
