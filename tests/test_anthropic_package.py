"""Documented native configuration runs through the installed CLI entry path."""
import json
import os
from pathlib import Path
import subprocess
import sys

import yaml

from prosaic_runtime import RuntimeConfig
from test_anthropic import native_server, response, tool
from test_anthropic_stream import events, sse

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / 'examples/anthropic.yml'


def test_native_example_loads_without_credentials_or_inference():
    config = RuntimeConfig.load(EXAMPLE)
    endpoint = config.profiles[config.default_profile]
    assert endpoint.provider == 'anthropic'
    assert endpoint.base_url == 'https://api.anthropic.com/v1'
    assert endpoint.api_key_env == 'ANTHROPIC_API_KEY'
    assert endpoint.temperature is None and endpoint.max_tokens > 0
    assert config.allowed_tools == frozenset({'read_file'})


def test_native_conformance_default_cli_never_loads_credentials(tmp_path):
    raw = yaml.safe_load(EXAMPLE.read_text())
    raw['profiles'][raw['default_profile']].update(base_url='http://localhost:9/v1',
        api_key_env=None, api_key_file=str(tmp_path / 'missing-secret'))
    config = tmp_path / 'native.yml'
    config.write_text(yaml.safe_dump(raw))
    result = subprocess.run([sys.executable, '-m', 'prosaic_runtime.cli', 'conformance',
        '--config', str(config), '--quiet'], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['qualification'] == 'not_qualified'


def test_native_documented_cli_text_and_tools(native_server, tmp_path):
    url, requests, responses = native_server
    raw = yaml.safe_load(EXAMPLE.read_text())
    endpoint = raw['profiles'][raw['default_profile']]
    endpoint.update(base_url=url, model='claude-test', features={'streaming': False})
    config = tmp_path / 'native.yml'
    config.write_text(yaml.safe_dump(raw))
    env = {**os.environ, 'PATH': str(Path(sys.executable).parent) + os.pathsep + os.environ.get('PATH', ''),
           'ANTHROPIC_API_KEY': 'fixture-key'}
    source = ROOT / 'examples/.prosaic'
    responses.append(response(text='native answer'))
    text = subprocess.run([sys.executable, '-m', 'prosaic_runtime.cli', 'subagents/native-summarizer.md',
        '--config', str(config), '--source', str(source), '--output', 'text', '--quiet'],
        env=env, capture_output=True, text=True, timeout=10)
    assert text.returncode == 0, text.stderr
    assert text.stdout == 'native answer\n'
    (tmp_path / 'input').write_text('wheel evidence')
    responses.extend([response(text='', calls=[tool(path='input')]), response(text='reviewed')])
    result = subprocess.run([sys.executable, '-m', 'prosaic_runtime.cli', 'subagents/reviewer.md',
        '--config', str(config), '--source', str(source), '--cwd', str(tmp_path),
        '--allow-tool', 'read_file', '--read-root', 'input', '--quiet'],
        env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value['stdout'] == 'reviewed' and value['token_usage'] == 14
    assert value['metadata']['provider'] == 'anthropic'
    assert all(r['path'] == '/v1/messages' for r in requests)


def test_native_smoke_runs_bundled_prose_and_reports_streaming(native_server, monkeypatch):
    from prosaic_runtime.diagnostics import smoke
    from test_anthropic import native_runtime, message
    url, requests, responses = native_server
    responses.extend([sse(events()), sse(events(message('', [tool(path='evidence/pilot.md')]))), sse(events())])
    monkeypatch.setenv('PATH', str(Path(sys.executable).parent) + os.pathsep + os.environ.get('PATH', ''))
    report = smoke(native_runtime(url, features={'streaming': True}).config, 'native')
    assert report['ok'] is True
    assert all(check['streaming'] is True for check in report['tests'].values())
    assert report['tests']['with_tools']['tool_execution'] is True
