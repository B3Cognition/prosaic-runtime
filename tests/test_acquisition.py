"""Acquisition must precede disclosure of final prose, without new authority."""
from dataclasses import replace
import json
import pytest
from prosaic_runtime import EndpointConfig, RunPolicy
from test_runtime import server, completion, artifact, runtime


def reply(content='done', calls=None, streaming=False):
    if not streaming:
        return completion(content, calls)
    parts = [{'choices': [{'delta': {'content': content, 'tool_calls': [dict(c, index=i) for i, c in enumerate(calls or [])]},
                          'finish_reason': 'tool_calls' if calls else 'stop'}]},
             {'choices': [], 'usage': {'total_tokens': 7}}]
    return ('text/event-stream', (''.join('data: ' + json.dumps(p) + '\n\n' for p in parts) + 'data: [DONE]\n\n').encode())


def read(path='input'):
    return [{'id': 'read', 'type': 'function', 'function': {'name': 'read_file', 'arguments': json.dumps({'path': path})}}]


def policy(**kwargs):
    return RunPolicy(allowed_tools=frozenset({'read_file'}), read_roots=('input',), initial_tool='read_file', **kwargs)


@pytest.mark.parametrize('streaming', [False, True])
def test_final_prose_and_arguments_only_disclosed_after_successful_acquisition(server, tmp_path, streaming):
    url, requests, responses = server
    (tmp_path / 'input').write_text('verified evidence')
    responses.extend([reply('', read(), streaming), reply('{"answer":"done"}', streaming=streaming)])
    acquisition = replace(artifact('read'), body='Read input using read_file. No final answer yet.')
    final = replace(artifact('read'), body='FINAL CONTRACT: {{args}}', resources=({'relPath': 'contract.md', 'content': 'FINAL RESOURCE'},))
    events = []
    result = runtime(EndpointConfig(url, 'test', features={'streaming': streaming, 'json_mode': True})).run(
        final, 'FINAL ARGUMENT', acquisition=acquisition, cwd=tmp_path, policy=policy(), on_event=events.append)
    assert result.exit_code == 0 and result.token_usage == 14
    assert len(requests) == 2
    first = json.dumps(requests[0])
    assert 'FINAL CONTRACT' not in first and 'FINAL ARGUMENT' not in first and 'FINAL RESOURCE' not in first
    assert 'Read input' in first and 'response_format' not in requests[0]
    assert requests[0]['tool_choice']['function']['name'] == 'read_file'
    second = requests[1]['messages']
    assert [m['role'] for m in second] == ['system', 'user', 'assistant', 'tool', 'user']
    assert 'verified evidence' in second[3]['content']
    assert 'FINAL CONTRACT: FINAL ARGUMENT' in second[4]['content'] and 'FINAL RESOURCE' in second[4]['content']
    assert requests[1]['response_format'] == {'type': 'json_object'}
    assert requests[1]['tool_choice'] == 'auto'
    assert result.metadata['acquisition_sha256'] == acquisition.digest
    assert any(e['event'] == 'acquisition_completed' for e in events)


@pytest.mark.parametrize('path', ['missing', 'private'])
def test_failed_acquisition_blocks_before_final_request(server, tmp_path, path):
    url, requests, responses = server
    (tmp_path / 'private').write_text('secret')
    responses.extend([completion('', read(path)), completion()])
    result = runtime(EndpointConfig(url, 'test', features={'streaming': False})).run(
        replace(artifact('read'), body='FINAL CONTRACT'), acquisition=artifact('read'), cwd=tmp_path, policy=policy())
    assert result.exit_code == 1 and result.stdout == ''
    assert result.metadata['failure_reason'] == 'acquisition_failed'
    assert result.token_usage == 7 and len(requests) == 1


@pytest.mark.parametrize('acquisition_tools,final_tools,initial', [('read', 'read', None), ('write', 'read', 'read_file'), ('read', '', 'read_file')])
def test_acquisition_requires_explicit_mutually_granted_tool(acquisition_tools, final_tools, initial):
    with pytest.raises(ValueError, match='acquisition|initial_tool'):
        runtime().run(artifact(final_tools), acquisition=artifact(acquisition_tools),
                      policy=RunPolicy(allowed_tools=frozenset({'read_file'}), initial_tool=initial))


def test_acquisition_consumes_shared_tool_budget(server, tmp_path):
    url, requests, responses = server
    (tmp_path / 'input').write_text('evidence')
    responses.extend([completion('', read()), completion('', read()), completion()])
    events = []
    result = runtime(EndpointConfig(url, 'test', features={'streaming': False})).run(
        artifact('read'), acquisition=artifact('read'), cwd=tmp_path, policy=policy(max_tool_rounds=1), on_event=events.append)
    assert result.exit_code == 1 and result.metadata['provider_error_code'] == 'tool_round_limit'
    assert len(requests) == 2 and sum(e['event'] == 'tool_started' for e in events) == 1


def test_cancel_after_acquisition_does_not_send_final_request(server, tmp_path):
    url, requests, responses = server
    (tmp_path / 'input').write_text('evidence')
    responses.extend([completion('', read()), completion()])
    stopped = []
    result = runtime(EndpointConfig(url, 'test', features={'streaming': False})).run(
        artifact('read'), acquisition=artifact('read'), cwd=tmp_path, policy=policy(),
        on_event=lambda e: stopped.append(True) if e['event'] == 'tool_completed' else None,
        cancelled=lambda: bool(stopped))
    assert result.exit_code == 130 and len(requests) == 1
    assert result.token_usage == 7


def test_cancelled_acquisition_without_reported_usage_remains_unknown(server, tmp_path):
    url, requests, responses = server
    (tmp_path / 'input').write_text('evidence')
    content_type, body = completion('', read())
    parsed = json.loads(body)
    parsed.pop('usage')
    responses.append((content_type, json.dumps(parsed).encode()))
    stopped = []
    result = runtime(EndpointConfig(url, 'test', features={'streaming': False})).run(
        artifact('read'), acquisition=artifact('read'), cwd=tmp_path, policy=policy(),
        on_event=lambda e: stopped.append(True) if e['event'] == 'tool_completed' else None,
        cancelled=lambda: bool(stopped))
    assert result.exit_code == 130 and len(requests) == 1
    assert result.token_usage is None  # No invented zero-usage receipt.


def test_acquisition_and_analysis_share_conversation_input_limit(server, tmp_path):
    url, requests, responses = server
    (tmp_path / 'input').write_text('evidence' * 1000)
    responses.extend([completion('', read()), completion()])
    result = runtime(EndpointConfig(url, 'test', features={'streaming': False})).run(
        artifact('read'), acquisition=artifact('read'), cwd=tmp_path, policy=policy(max_input_bytes=5000))
    assert result.exit_code == 1 and result.metadata['failure_reason'] == 'budget_exceeded'
    assert len(requests) == 1 and result.token_usage == 7


def test_acquisition_does_not_reset_deadline(server, tmp_path, monkeypatch):
    import time
    url, requests, responses = server
    (tmp_path / 'input').write_text('evidence')
    responses.extend([completion('', read()), completion()])
    real_monotonic = time.monotonic
    offset = [0]
    monkeypatch.setattr(time, 'monotonic', lambda: real_monotonic() + offset[0])
    def event(e):
        if e['event'] == 'tool_completed':
            offset[0] = 10
    result = runtime(EndpointConfig(url, 'test', features={'streaming': False})).run(
        artifact('read'), acquisition=artifact('read'), cwd=tmp_path, policy=policy(timeout_s=5), on_event=event)
    assert result.timed_out and len(requests) == 1 and result.token_usage == 7


def test_expired_inspection_budget_does_not_start_second_inspection(monkeypatch):
    import prosaic_runtime.runtime as module
    import time
    real_monotonic = time.monotonic
    offset = [0]
    monkeypatch.setattr(time, 'monotonic', lambda: real_monotonic() + offset[0])
    def inspect(identifier, *args, **kwargs):
        # Stand-in for an external Prosaic process that used up the deadline.
        if identifier != 'final':
            raise AssertionError('second inspection began after timeout')
        offset[0] = 10
        return artifact('read')
    monkeypatch.setattr(module, 'inspect_artifact', inspect)
    result = runtime().run('final', acquisition='acquire', policy=policy(timeout_s=5))
    assert result.timed_out and result.exit_code == 1


@pytest.mark.parametrize('phase', ['final', 'acquisition'])
def test_inspection_timeout_is_a_runtime_result_not_an_uncaught_exception(tmp_path, phase):
    executable = tmp_path / 'slow-prosaic'
    executable.write_text('#!/usr/bin/env python3\nimport time\ntime.sleep(1)\n')
    executable.chmod(0o700)
    engine = runtime(executable=str(executable))
    result = engine.run('subagents/slow.md' if phase == 'final' else artifact('read'),
        acquisition='subagents/slow.md' if phase == 'acquisition' else None,
        policy=policy(timeout_s=0.05))
    assert result.exit_code == 1 and result.timed_out
    assert result.metadata['failure_reason'] == 'inspection_timeout'


@pytest.mark.parametrize('key,value', [('model_tier', 'strong'), ('effort', 'high')])
def test_acquisition_cannot_reroute_or_override_final_effort(key, value):
    with pytest.raises(ValueError, match=f'acquisition {key}'):
        runtime().run(artifact('read'), acquisition=artifact('read', **{key: value}), policy=policy())


def test_runnable_example_uses_native_read_then_explicit_preloading(server):
    import os
    from pathlib import Path
    import shutil
    import subprocess
    import sys
    if not shutil.which('prosaic'):
        pytest.skip('Prosaic CLI required')
    url, requests, responses = server
    responses.extend([completion('', read('evidence/pilot.md')), completion('Observed: S1. Unknown: cause. Next check: logs.'),
                      completion('Observed: S1. Unknown: cause. Next check: logs.')])
    script = Path(__file__).resolve().parents[1] / 'examples/run_acquisition.py'
    process = subprocess.run([sys.executable, str(script), '--base-url', url, '--model', 'test', '--no-stream'],
        capture_output=True, text=True, timeout=20, env={**os.environ, 'LOCAL_LLM_API_KEY': 'test'})
    assert process.returncode == 0, process.stderr
    rows = [json.loads(line) for line in process.stdout.splitlines()]
    assert [row['mode'] for row in rows] == ['staged', 'preloaded']
    assert len(requests) == 3
    assert '120 requests' not in json.dumps(requests[0]['messages'])
    assert '120 requests' in json.dumps(requests[1]['messages'])
    assert 'tools' not in requests[2] and '120 requests' in json.dumps(requests[2]['messages'])
