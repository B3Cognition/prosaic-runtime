"""Real executable fixtures exercise discovery, confinement and process cleanup."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import time

import pytest
import yaml
from prosaic_runtime import EndpointConfig, ProsaicRuntime, RunPolicy, RuntimeConfig
from test_runtime import server, artifact, completion
from test_acquisition import reply


def manifest(tmp_path, code='print(json.dumps({"argv": sys.argv[1:]}))', **changes):
    tools = tmp_path / 'tools'
    tools.mkdir(exist_ok=True)
    executable = tmp_path / 'analyzer'
    executable.write_text(f'#!{sys.executable}\nimport json, os, sys, time\n'
        'if sys.argv[1:] == ["--version"]:\n    print("analyzer 1.0"); sys.exit(0)\n' + code + '\n')
    executable.chmod(0o755)
    data = {'schema_version': 1, 'name': 'analyze_spec', 'tool_version': '1.0',
        'description': 'Analyze a spec', 'executable': str(executable), 'argv': ['{spec}', '--json'],
        'parameters': {'type': 'object', 'additionalProperties': False, 'required': ['spec'],
                       'properties': {'spec': {'type': 'string'}}},
        'path_parameters': {'spec': 'read'}, 'version_probe': ['--version'],
        'output_format': 'json', 'timeout_s': 1, 'max_output_bytes': 1024}
    data.update(changes)
    path = tools / 'analyzer.yml'
    path.write_text(yaml.safe_dump(data))
    return tools, executable, path


def config(tmp_path, url, directories, **changes):
    path = tmp_path / 'runtime.yml'
    data = {'default_profile': 'local', 'routes': {'fast': 'local'}, 'allowed_tools': ['analyze_spec'],
        'tool_directories': [str(p) for p in directories],
        'profiles': {'local': {'base_url': url, 'model': 'test', 'features': {'streaming': False}}}}
    data.update(changes)
    path.write_text(yaml.safe_dump(data))
    return RuntimeConfig.load(path)


def policy(**changes):
    return RunPolicy(**({'allowed_tools': frozenset({'analyze_spec'}), 'read_roots': ('.',)} | changes))


def native_call(spec='spec.md'):
    return {'id': 'analysis', 'type': 'function', 'function': {
        'name': 'analyze_spec', 'arguments': json.dumps({'spec': spec})}}


@pytest.mark.parametrize('streaming', [False, True])
def test_manifest_native_call_executes_real_cli_without_shell(server, tmp_path, streaming):
    url, requests, responses = server
    tools, _, _ = manifest(tmp_path)
    cfg = config(tmp_path, url, [tools])
    cfg = replace(cfg, profiles={'local': replace(cfg.profiles['local'], features={'streaming': streaming})})
    spec = 'spec; $(touch pwned).md'
    (tmp_path / spec).write_text('requirements')
    responses.extend([reply('', [native_call(spec)], streaming), reply('done', streaming=streaming)])
    events = []
    runtime = ProsaicRuntime(cfg)
    result = runtime.run(artifact(['analyze_spec']), cwd=tmp_path, policy=policy(), on_event=events.append)
    assert result.exit_code == 0
    message = next(m for m in requests[1]['messages'] if m['role'] == 'tool')
    assert json.loads(message['content']) == {'status': 'ok', 'result': {'argv': [str(tmp_path / spec), '--json']}}
    assert not (tmp_path / 'pwned').exists()
    event = next(e for e in events if e['event'] == 'tool_completed')
    assert event['status'] == 'ok' and event['tool_version'] == runtime.tool_descriptors['analyze_spec']['version']


@pytest.mark.parametrize('missing', ['executable', 'config_grant', 'host_grant', 'read_scope'])
def test_cli_preflight_fails_before_endpoint_request(server, tmp_path, missing):
    url, requests, _ = server
    tools, executable, _ = manifest(tmp_path)
    cfg = config(tmp_path, url, [tools], allowed_tools=[] if missing == 'config_grant' else ['analyze_spec'])
    if missing == 'executable':
        executable.unlink()
    host = policy(allowed_tools=frozenset() if missing == 'host_grant' else frozenset({'analyze_spec'}),
                  read_roots=() if missing == 'read_scope' else ('.',))
    runtime = ProsaicRuntime(cfg)
    report = runtime.preflight(artifact(['analyze_spec']), cwd=tmp_path, policy=host)
    assert report['ok'] is False and report['checks']['analyze_spec']['status'] == 'error'
    with pytest.raises(ValueError, match='preflight'):
        runtime.run(artifact(['analyze_spec']), cwd=tmp_path, policy=host)
    assert requests == []


def test_unused_missing_executable_does_not_block_selected_agent(server, tmp_path):
    url, requests, responses = server
    tools, executable, _ = manifest(tmp_path)
    executable.unlink()
    runtime = ProsaicRuntime(config(tmp_path, url, [tools]))
    assert runtime.preflight(artifact(), cwd=tmp_path)['ok']
    assert not runtime.preflight(all_tools=True, cwd=tmp_path, policy=policy())['ok']
    responses.append(completion('done'))
    assert runtime.run(artifact(), cwd=tmp_path).exit_code == 0
    assert len(requests) == 1


@pytest.mark.parametrize('kind', ['outside', 'symlink', 'missing', 'forbidden', 'directory'])
def test_path_parameters_deny_outside_or_invalid_files(server, tmp_path, kind):
    url, requests, responses = server
    marker = tmp_path / 'executed'
    tools, _, _ = manifest(tmp_path, code=f'open({str(marker)!r}, "w").write("bad"); print("{{}}")')
    root = tmp_path / 'workspace'; root.mkdir()
    outside = tmp_path / 'private.md'; outside.write_text('private')
    (root / 'link').symlink_to(outside)
    (root / 'private.md').write_text('private')
    paths = {'outside': '../private.md', 'symlink': 'link', 'missing': 'missing.md',
             'forbidden': 'private.md', 'directory': '.'}
    responses.extend([completion('', [native_call(paths[kind])]), completion('done')])
    runtime = ProsaicRuntime(config(tmp_path, url, [tools]))
    runtime.run(artifact(['analyze_spec']), cwd=root, policy=policy(forbidden_roots=('private.md',)))
    tool_message = next(m for m in requests[1]['messages'] if m['role'] == 'tool')
    assert json.loads(tool_message['content'])['error'] == 'cli_path_denied'
    assert not marker.exists()


@pytest.mark.parametrize('code,expected', [
    ('print("not json")', 'cli_invalid_output'),
    ('print("{\\"x\\":1,\\"x\\":2}")', 'cli_invalid_output'),
    ('print("NaN")', 'cli_invalid_output'),
    ('print("SECRET", file=sys.stderr); sys.exit(2)', 'cli_exit'),
    ('print("x" * 4096)', 'cli_output_limit'),
    ('print("x" * 4096, file=sys.stderr)', 'cli_output_limit'),
    ('time.sleep(3)', 'cli_timeout'),
])
def test_cli_failures_are_bounded_and_redacted(server, tmp_path, code, expected):
    url, requests, responses = server
    tools, _, _ = manifest(tmp_path, code=code, timeout_s=0.75)
    (tmp_path / 'spec.md').write_text('input')
    responses.extend([completion('', [native_call()]), completion('done')])
    events = []
    started = time.monotonic()
    result = ProsaicRuntime(config(tmp_path, url, [tools])).run(artifact(['analyze_spec']), cwd=tmp_path,
        policy=policy(), on_event=events.append)
    assert time.monotonic() - started < 2
    tool_message = next(m for m in requests[1]['messages'] if m['role'] == 'tool')
    assert json.loads(tool_message['content']) == {'status': 'error', 'error': expected}
    assert 'SECRET' not in json.dumps(events) + result.stderr


def test_cli_does_not_inherit_credentials_unless_explicit(server, tmp_path, monkeypatch):
    url, requests, responses = server
    monkeypatch.setenv('SYNTHETIC_API_KEY', 'private-value')
    tools, _, _ = manifest(tmp_path, code='print(json.dumps({"secret": os.getenv("SYNTHETIC_API_KEY")}))')
    (tmp_path / 'spec.md').write_text('input')
    responses.extend([completion('', [native_call()]), completion('done')])
    ProsaicRuntime(config(tmp_path, url, [tools])).run(artifact(['analyze_spec']), cwd=tmp_path, policy=policy())
    message = next(m for m in requests[1]['messages'] if m['role'] == 'tool')
    assert json.loads(message['content'])['result'] == {'secret': None}


@pytest.mark.parametrize('change', [
    {'argv': ['{unknown}']}, {'argv': ['--spec={spec}']}, {'executable': '{spec}'},
    {'schema_version': True}, {'unexpected': 1}, {'timeout_s': float('nan')},
    {'parameters': {'type': 'object'}}, {'path_parameters': {'unknown': 'read'}},
])
def test_invalid_manifest_rejected(tmp_path, change):
    tools, _, _ = manifest(tmp_path, **change)
    with pytest.raises(ValueError, match='manifest'):
        ProsaicRuntime(config(tmp_path, 'http://localhost:9/v1', [tools]))


def test_discovery_requires_explicit_trust_and_rejects_symlink_manifests(tmp_path):
    tools, _, path = manifest(tmp_path)
    untrusted = ProsaicRuntime(config(tmp_path, 'http://localhost:9/v1', []))
    assert 'analyze_spec' not in untrusted.tool_descriptors
    outside = tmp_path / 'outside.yml'; path.rename(outside); path.symlink_to(outside)
    with pytest.raises(ValueError, match='manifest'):
        ProsaicRuntime(config(tmp_path, 'http://localhost:9/v1', [tools]))


def test_manifest_duplicate_keys_and_names_rejected(tmp_path):
    tools, _, path = manifest(tmp_path)
    original = path.read_text()
    path.write_text(original + '\nname: other\n')
    with pytest.raises(ValueError, match='manifest'):
        ProsaicRuntime(config(tmp_path, 'http://localhost:9/v1', [tools]))
    path.write_text(original)
    (tools / 'duplicate.yaml').write_text(original)
    with pytest.raises(ValueError, match='duplicate'):
        ProsaicRuntime(config(tmp_path, 'http://localhost:9/v1', [tools]))


def test_relative_trust_directories_resolve_against_config_not_cwd(tmp_path):
    tools, _, _ = manifest(tmp_path)
    cfg = config(tmp_path, 'http://localhost:9/v1', [Path('tools')])
    assert cfg.tool_directories == (str(tools),)
    assert 'analyze_spec' in ProsaicRuntime(cfg).tool_descriptors
