import json
import os
from pathlib import Path
import sys
import time

import pytest


@pytest.fixture
def executable(tmp_path):
    def make(events=(), *, delay=0, exit_code=0, raw=None, name='fixture'):
        path = tmp_path / name
        output = raw if raw is not None else ''.join(json.dumps(e) + '\n' for e in events)
        path.write_text(f'#!{sys.executable}\nimport sys, time\n'
                        'prompt = sys.stdin.read()\n'
                        f'sys.stdout.write({output!r})\nsys.stdout.flush()\n'
                        f'time.sleep({delay!r})\nsys.exit({exit_code!r})\n')
        path.chmod(0o755)
        return str(path)
    return make


def execute(executable, events, *, backend='claude-code', **kwargs):
    from prosaic_runtime import NativeProfile, NativePolicy
    from prosaic_runtime.native_backends import execute_native
    profile = NativeProfile(backend, 'example', 'account', executable(events, **kwargs))
    return execute_native(profile, 'task', cwd=Path.cwd(), policy=NativePolicy(),
                          deadline=time.monotonic() + 2, env=dict(os.environ))


def test_claude_terminal_success_and_usage(executable):
    result = execute(executable, [
        {'type': 'system', 'subtype': 'init', 'session_id': 's'},
        {'type': 'result', 'subtype': 'success', 'is_error': False,
         'result': 'done', 'session_id': 's', 'num_turns': 1,
         'usage': {'input_tokens': 5, 'output_tokens': 2, 'cache_read_input_tokens': 3}},
    ])
    assert result.exit_code == 0 and result.stdout == 'done'
    assert result.token_usage == 10
    assert result.metadata['session_id'] == 's'


def test_codex_terminal_success_and_usage(executable):
    result = execute(executable, [
        {'type': 'thread.started', 'thread_id': 'thread'},
        {'type': 'turn.started'},
        {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'done'}},
        {'type': 'turn.completed', 'usage': {'input_tokens': 5, 'output_tokens': 2,
                                            'cached_input_tokens': 3}},
    ], backend='codex-cli')
    assert result.exit_code == 0 and result.stdout == 'done'
    assert result.token_usage == 7  # Cached inputs are already included by Codex.


def test_claude_limit_before_work_is_safe_to_fallback(executable):
    result = execute(executable, [
        {'type': 'rate_limit_event', 'rate_limit_info': {'status': 'rejected', 'resetsAt': 2000000000}},
        {'type': 'result', 'subtype': 'error_during_execution', 'is_error': True,
         'num_turns': 0, 'result': 'limit'},
    ], exit_code=1)
    assert result.metadata['failure_reason'] == 'usage_limit'
    assert result.metadata['safe_to_fallback'] is True
    assert result.metadata['retry_at'] == 2000000000


@pytest.mark.parametrize('backend,partial', [
    ('codex-cli', {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'partial'}}),
    ('codex-cli', {'type': 'item.completed', 'item': {'type': 'reasoning', 'text': 'partial'}}),
    ('claude-code', {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'partial'}]}}),
    ('claude-code', {'type': 'assistant', 'message': {'content': [{'type': 'thinking', 'thinking': 'partial'}]}}),
    ('claude-code', {'type': 'assistant', 'message': {'content': [{'type': 'redacted_thinking', 'data': 'opaque'}]}}),
    ('claude-code', {'type': 'stream_event', 'event': {'type': 'content_block_start',
        'content_block': {'type': 'redacted_thinking', 'data': 'opaque'}}}),
    ('claude-code', {'type': 'stream_event', 'event': {'type': 'content_block_start',
        'content_block': {'type': 'future_tool'}}}),
    ('claude-code', {'type': 'stream_event', 'event': {'type': 'content_block_delta',
        'delta': {'type': 'input_json_delta', 'partial_json': '{}'}}}),
    ('claude-code', {'type': 'stream_event', 'event': {'type': 'message_start'}}),
])
def test_model_work_before_quota_error_cannot_authorize_replay(executable, backend, partial):
    terminal = ([{'type': 'turn.failed', 'error': {'code': 'usage_limit_reached'}}]
        if backend == 'codex-cli' else [
            {'type': 'rate_limit_event', 'rate_limit_info': {'status': 'rejected'}},
            {'type': 'result', 'is_error': True, 'num_turns': 0}])
    result = execute(executable, [partial, *terminal], backend=backend, exit_code=1)
    assert result.exit_code != 0
    assert result.metadata['work_started'] is True
    assert result.metadata['safe_to_fallback'] is False
    assert result.metadata['effects_uncertain'] is True


def test_limit_after_tool_use_requires_recovery(executable):
    result = execute(executable, [
        {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Edit'}]}},
        {'type': 'rate_limit_event', 'rate_limit_info': {'status': 'rejected'}},
        {'type': 'result', 'is_error': True, 'num_turns': 1},
    ], exit_code=1)
    assert result.metadata['failure_reason'] == 'usage_limit'
    assert result.metadata['safe_to_fallback'] is False
    assert result.metadata['effects_uncertain'] is True


@pytest.mark.parametrize('events,raw', [
    ([{'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'looks done'}]}}], None),
    ([], '{malformed}\n'),
    ([], '[]\n'),
    ([{'type': 'result', 'is_error': False, 'result': 'done'}], '{"type":"result","is_error":false,"result":"done"}\n{bad}\n'),
])
def test_missing_or_malformed_completion_is_not_success(executable, events, raw):
    result = execute(executable, events, raw=raw)
    assert result.exit_code != 0
    assert result.metadata['safe_to_fallback'] is False


def test_codex_structured_limit_is_distinct_from_auth_failure(executable):
    for code, expected in [('usage_limit_reached', 'usage_limit'), ('authentication_failed', 'authentication_failed')]:
        result = execute(executable, [{'type': 'turn.failed', 'error': {'code': code, 'message': 'failure'}}],
                         backend='codex-cli', exit_code=1)
        assert result.metadata['failure_reason'] == expected
        assert result.metadata['safe_to_fallback'] is (expected == 'usage_limit')


def test_timeout_and_output_limit(executable):
    from prosaic_runtime import NativePolicy, NativeProfile
    from prosaic_runtime.native_backends import execute_native
    for binary, policy, reason in [
        (executable(delay=5), NativePolicy(timeout_s=.1), 'timeout'),
        (executable(raw='x' * 10000, name='large'), NativePolicy(max_output_bytes=100), 'output_limit'),
    ]:
        result = execute_native(NativeProfile('claude-code', 'test', 'a', binary), 'task',
            cwd=Path.cwd(), policy=policy, deadline=time.monotonic() + policy.timeout_s, env=dict(os.environ))
        assert result.exit_code != 0 and result.metadata['failure_reason'] == reason
        assert result.metadata['safe_to_fallback'] is False


def test_cancellation_and_callback_exception_cleanup(executable):
    from prosaic_runtime import NativePolicy, NativeProfile
    from prosaic_runtime.native_backends import execute_native
    profile = NativeProfile('codex-cli', 'test', 'a', executable([
        {'type': 'thread.started', 'thread_id': 't'}], delay=5))
    started = time.monotonic()
    result = execute_native(profile, 'task', cwd=Path.cwd(), policy=NativePolicy(),
        deadline=started + 10, env=dict(os.environ), cancelled=lambda: time.monotonic() - started > .1)
    assert result.exit_code == 130 and result.metadata['safe_to_fallback'] is False
    def broken(event):
        raise RuntimeError('critical sink failed')
    with pytest.raises(RuntimeError, match='critical sink'):
        execute_native(profile, 'task', cwd=Path.cwd(), policy=NativePolicy(),
                       deadline=time.monotonic() + 10, env=dict(os.environ), on_event=broken)
    assert time.monotonic() - started < 3


def test_codex_real_cli_message_only_limit(executable):
    # Codex exec ThreadErrorEvent currently carries message, without a code.
    result = execute(executable, [
        {'type': 'error', 'message': "You've hit your usage limit. Try again later."},
        {'type': 'turn.failed', 'error': {'message': "You've hit your usage limit. Try again later."}},
    ], backend='codex-cli', exit_code=1)
    assert result.metadata['failure_reason'] == 'usage_limit'
    assert result.metadata['safe_to_fallback'] is True


def test_hook_activity_before_limit_prevents_replay(executable):
    result = execute(executable, [
        {'type': 'system', 'subtype': 'hook_started'},
        {'type': 'rate_limit_event', 'rate_limit_info': {'status': 'rejected'}},
        {'type': 'result', 'is_error': True, 'num_turns': 0},
    ], exit_code=1)
    assert result.metadata['safe_to_fallback'] is False


def test_malformed_nested_stream_event_is_failure(executable):
    result = execute(executable, [{'type': 'stream_event', 'event': {'content_block': None}}])
    assert result.exit_code != 0 and result.metadata['safe_to_fallback'] is False


def test_no_launch_has_known_zero_usage(tmp_path):
    from prosaic_runtime import NativePolicy, NativeProfile
    from prosaic_runtime.native_backends import execute_native
    result = execute_native(NativeProfile('codex-cli', 'test', 'a', str(tmp_path / 'missing')),
        'task', cwd=tmp_path, policy=NativePolicy(), deadline=time.monotonic() + 1, env=dict(os.environ))
    assert result.token_usage == 0 and result.metadata['safe_to_fallback'] is True


def test_native_argv_and_private_config_directory(tmp_path):
    from prosaic_runtime import NativeProfile, NativePolicy
    from prosaic_runtime.native_backends import execute_native
    path = tmp_path / 'argv'
    path.write_text(f'#!{sys.executable}\nimport os,sys,json\n'
        'sys.stdin.read()\n'
        'print(json.dumps({"type":"result","subtype":"success","is_error":False,'
        '"result":json.dumps({"argv":sys.argv[1:],"home":os.environ.get("CLAUDE_CONFIG_DIR")})}))\n')
    path.chmod(0o755)
    profile = NativeProfile('claude-code', 'sonnet', 'a', str(path), config_dir=str(tmp_path / 'account'))
    result = execute_native(profile, 'secret prompt', cwd=tmp_path, policy=NativePolicy(),
                            deadline=time.monotonic() + 1, env=dict(os.environ))
    captured = json.loads(result.stdout)
    assert captured['home'] == str(tmp_path / 'account')
    assert '--restricted' in captured['argv'] and 'dontAsk' in captured['argv']
    assert 'secret prompt' not in captured['argv']
    assert captured['argv'][captured['argv'].index('--tools') + 1] == 'Read,Glob,Grep'


def test_cancellation_when_child_closes_its_pipes(tmp_path):
    from prosaic_runtime.native_process import run_process
    path = tmp_path / 'closed'
    path.write_text(f'#!{sys.executable}\nimport os,time\nos.close(0)\nos.close(1)\nos.close(2)\ntime.sleep(5)\n')
    path.chmod(0o755)
    started = time.monotonic()
    result = run_process([str(path)], '', cwd=tmp_path, env=dict(os.environ), deadline=started + .6,
        max_output_bytes=1000, on_message=lambda e: None,
        cancelled=lambda: time.monotonic() - started > .1)
    assert result.failure == 'cancelled'


def test_timeout_stops_owned_descendants(tmp_path):
    from prosaic_runtime.native_process import run_process
    path = tmp_path / 'descendants'
    marker = tmp_path / 'escaped'
    path.write_text(f'#!{sys.executable}\nimport os,time\n'
        'if os.fork() == 0:\n'
        f'    time.sleep(.5)\n    open({str(marker)!r},"w").write("escaped")\n'
        '    os._exit(0)\n'
        'time.sleep(5)\n')
    path.chmod(0o755)
    result = run_process([str(path)], '', cwd=tmp_path, env=dict(os.environ),
        deadline=time.monotonic() + .15, max_output_bytes=1000, on_message=lambda e: None)
    assert result.failure == 'timeout'
    time.sleep(.55)
    assert not marker.exists()


@pytest.mark.parametrize('extra', [
    {'type': 'assistant', 'message': {'content': [{}]}},
    {'type': 'stream_event', 'event': {}},
])
def test_unknown_nested_events_cannot_prove_safe_replay(executable, extra):
    result = execute(executable, [extra,
        {'type': 'rate_limit_event', 'rate_limit_info': {'status': 'rejected'}},
        {'type': 'result', 'is_error': True, 'num_turns': 0}], exit_code=1)
    assert result.metadata['safe_to_fallback'] is False


def test_auth_failure_cannot_be_overwritten_by_later_limit(executable):
    result = execute(executable, [
        {'type': 'error', 'error': {'code': 'authentication_failed'}},
        {'type': 'turn.failed', 'error': {'code': 'usage_limit_reached'}},
    ], backend='codex-cli', exit_code=1)
    assert result.metadata['safe_to_fallback'] is False
    assert result.metadata['failure_reason'] == 'authentication_failed'


def test_duplicate_json_fields_are_not_success(executable):
    result = execute(executable, [], raw='{"type":"result","subtype":"success","is_error":true,"is_error":false,"result":"done"}\n')
    assert result.exit_code != 0 and result.metadata['safe_to_fallback'] is False


def test_malformed_terminal_cannot_authorize_fallback(executable):
    result = execute(executable, [
        {'type': 'rate_limit_event', 'rate_limit_info': {'status': 'rejected'}},
        {'type': 'result', 'num_turns': 0}], exit_code=1)
    assert result.metadata['safe_to_fallback'] is False
