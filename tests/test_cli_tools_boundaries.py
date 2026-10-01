import json
import os
import subprocess
import sys
import time
import pytest
from prosaic_runtime import CustomTool, ProsaicRuntime
from test_runtime import server, artifact, completion
from test_cli_tools import manifest, config, policy, native_call


@pytest.mark.parametrize('kind', ['cancel', 'deadline', 'tool_timeout'])
def test_running_cli_is_stopped_with_child_process_cleanup(server, tmp_path, kind):
    url, requests, responses = server
    marker = tmp_path / 'started'
    late = tmp_path / 'late'
    # Nested quoting is constructed independently as literal argv for the child.
    child_code = f'import time; time.sleep(1.5); open({str(late)!r}, "w").write("bad")'
    code = f'import subprocess\nsubprocess.Popen([sys.executable, "-c", {child_code!r}])\nopen({str(marker)!r}, "w").write("started")\ntime.sleep(4)'
    tools, _, _ = manifest(tmp_path, code=code, version_probe=[], timeout_s=0.75 if kind == 'tool_timeout' else 5)
    (tmp_path / 'spec.md').write_text('input')
    responses.extend([completion('', [native_call()]), completion('done')])
    host = policy(timeout_s=0.75 if kind == 'deadline' else 10, initial_tool='analyze_spec')
    result = ProsaicRuntime(config(tmp_path, url, [tools])).run(artifact(['analyze_spec']), cwd=tmp_path,
        policy=host, cancelled=lambda: kind == 'cancel' and marker.exists())
    assert marker.exists() and len(requests) == (2 if kind == 'tool_timeout' else 1)
    assert result.exit_code == (130 if kind == 'cancel' else 0 if kind == 'tool_timeout' else 1)
    assert result.timed_out == (kind == 'deadline')
    assert result.token_usage == (14 if kind == 'tool_timeout' else 7)
    if kind == 'tool_timeout':
        content = next(m['content'] for m in requests[1]['messages'] if m['role'] == 'tool')
        assert json.loads(content) == {'status': 'error', 'error': 'cli_timeout'}
    time.sleep(1.6)
    assert not late.exists()


@pytest.mark.parametrize('change', [
    {'pass_env': ['SYNTHETIC_API_KEY']}, {'success_exit_codes': [0, 1]}, {'version_contains': 'wrong'},
])
def test_environment_exit_code_and_version_contracts(server, tmp_path, monkeypatch, change):
    url, requests, responses = server
    monkeypatch.setenv('SYNTHETIC_API_KEY', 'synthetic-value')
    code = 'print(json.dumps({"present": os.getenv("SYNTHETIC_API_KEY") is not None})); sys.exit(' + ('0' if 'pass_env' in change else '1') + ')'
    tools, _, _ = manifest(tmp_path, code=code, **change)
    runtime = ProsaicRuntime(config(tmp_path, url, [tools]))
    if 'version_contains' in change:
        assert runtime.preflight(artifact(['analyze_spec']), cwd=tmp_path, policy=policy())['checks']['analyze_spec']['message'] == 'cli_version'
        assert requests == []
    else:
        (tmp_path / 'spec.md').write_text('input')
        responses.extend([completion('', [native_call()]), completion('done')])
        runtime.run(artifact(['analyze_spec']), cwd=tmp_path, policy=policy())
        result = json.loads(next(m['content'] for m in requests[1]['messages'] if m['role'] == 'tool'))
        assert result == {'status': 'ok', 'result': {'present': 'pass_env' in change}}


def test_plain_arguments_cannot_become_options(server, tmp_path):
    url, requests, responses = server
    tools, _, _ = manifest(tmp_path, path_parameters={})
    responses.extend([completion('', [native_call('--version')]), completion('done')])
    ProsaicRuntime(config(tmp_path, url, [tools])).run(artifact(['analyze_spec']), cwd=tmp_path, policy=policy())
    result = json.loads(next(m['content'] for m in requests[1]['messages'] if m['role'] == 'tool'))
    assert result == {'status': 'error', 'error': 'cli_arguments'}


def test_registry_collision_and_manifest_limit(tmp_path):
    tools, _, path = manifest(tmp_path)
    cfg = config(tmp_path, 'http://localhost:9/v1', [tools])
    custom = CustomTool('analyze_spec', 'Duplicate', {'type': 'object', 'additionalProperties': False}, lambda _: {}, 'v1')
    with pytest.raises(ValueError, match='duplicate'):
        ProsaicRuntime(cfg, custom_tools={'analyze_spec': custom})
    path.write_text(' ' * 65537)
    with pytest.raises(ValueError, match='manifest'):
        ProsaicRuntime(cfg)


def test_public_cli_preflight_never_requests_inference(server, tmp_path):
    url, requests, _ = server
    tools, _, _ = manifest(tmp_path, path_parameters={})
    config(tmp_path, url, [tools])
    command = [sys.executable, '-m', 'prosaic_runtime.cli', 'preflight', '--all-tools',
               '--config', str(tmp_path / 'runtime.yml'), '--allow-tool', 'analyze_spec', '--cwd', str(tmp_path)]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)['inference'] is False and requests == []


@pytest.mark.skipif(os.name != 'posix', reason='FIFO fixture requires POSIX')
def test_nonregular_manifest_rejected_without_blocking(tmp_path):
    tools, _, path = manifest(tmp_path)
    path.unlink(); os.mkfifo(path)
    config(tmp_path, 'http://localhost:9/v1', [tools])
    completed = subprocess.run([sys.executable, '-m', 'prosaic_runtime.cli', 'preflight', '--all-tools',
        '--config', str(tmp_path / 'runtime.yml')], capture_output=True, text=True, timeout=2)
    assert completed.returncode == 2 and 'manifest' in completed.stderr


def test_relative_path_lookup_is_pinned_before_invocation_cwd_changes(server, tmp_path, monkeypatch):
    url, requests, responses = server
    tools, _, _ = manifest(tmp_path, code='print(json.dumps({"source":"trusted"}))', executable='analyzer')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('PATH', '.' + os.pathsep + os.environ.get('PATH', ''))
    runtime = ProsaicRuntime(config(tmp_path, url, [tools]))
    workspace = tmp_path / 'workspace'; workspace.mkdir()
    (workspace / 'spec.md').write_text('input')
    decoy = workspace / 'analyzer'
    decoy.write_text(f'#!{sys.executable}\nimport json, sys\n'
        'if sys.argv[1:] == ["--version"]: print("analyzer 1.0")\n'
        'else: print(json.dumps({"source":"decoy"}))\n')
    decoy.chmod(0o755)
    responses.extend([completion('', [native_call()]), completion('done')])
    runtime.run(artifact(['analyze_spec']), cwd=workspace, policy=policy())
    result = json.loads(next(m['content'] for m in requests[1]['messages'] if m['role'] == 'tool'))
    assert result == {'status': 'ok', 'result': {'source': 'trusted'}}


def test_preflight_shared_deadline_has_a_safe_diagnostic(tmp_path):
    tools, _, _ = manifest(tmp_path, code='time.sleep(3); print("{}")',
                           version_probe=['--slow-version'], timeout_s=5, path_parameters={})
    config(tmp_path, 'http://localhost:9/v1', [tools])
    completed = subprocess.run([sys.executable, '-m', 'prosaic_runtime.cli', 'preflight', '--all-tools',
        '--config', str(tmp_path / 'runtime.yml'), '--allow-tool', 'analyze_spec', '--timeout', '0.1'],
        capture_output=True, text=True, timeout=3)
    assert completed.returncode != 0
    assert 'deadline' in json.loads(completed.stderr)['error']
