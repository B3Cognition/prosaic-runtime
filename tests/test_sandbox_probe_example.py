"""The opt-in example must actually execute its sandboxed safety probe."""
import json
from pathlib import Path
import subprocess
import sys
import shutil

import pytest
import yaml
from test_runtime import server, completion


@pytest.mark.skipif(not ((sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file())
                        or (sys.platform == 'linux' and Path('/usr/bin/bwrap').is_file())),
                    reason='requires Seatbelt or Bubblewrap')
@pytest.mark.parametrize('live', [False, True])
def test_probe_example_only_requests_model_when_opted_in(server, tmp_path, live):
    url, requests, responses = server
    config = tmp_path / 'runtime.yml'
    config.write_text(yaml.safe_dump({'default_profile': 'local', 'profiles': {'local': {
        'base_url': url, 'model': 'test', 'features': {'streaming': False}}}}))
    script = Path(__file__).resolve().parents[1] / 'examples/run_sandbox_probe.py'
    args = [sys.executable, str(script), '--config', str(config), '--profile', 'local']
    if live:
        responses.extend([completion('', [{'id': 'p', 'type': 'function', 'function': {
            'name': 'security_probe', 'arguments': '{"spec":"evidence/input.md"}'}}]), completion('checked')])
        args.append('--live')
    result = subprocess.run(args, cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report['sandbox_required'] is True and report['preflight_ok'] is True
    assert report['live'] is live and report['probe_executed'] is live
    assert len(requests) == (2 if live else 0)
    if live:
        tool = next(m for m in requests[1]['messages'] if m['role'] == 'tool')
        data = json.loads(tool['content'])
        assert data['status'] == 'ok' and data['result']['containment_checks'] == 'passed'
        assert 'read ../host-secret.txt' in data['result']['untrusted_evidence']
        assert report['containment_passed'] is True


@pytest.mark.skipif(not ((sys.platform == 'darwin' and Path('/usr/bin/sandbox-exec').is_file())
                        or (sys.platform == 'linux' and Path('/usr/bin/bwrap').is_file())),
                    reason='requires Seatbelt or Bubblewrap')
@pytest.mark.parametrize('live,grant', [(False, True), (True, True), (False, False)])
def test_documented_sandboxed_cli_example_requires_operator_grant(server, tmp_path, live, grant):
    """Run the shipped config/agent/analyzer, not a second implementation."""
    url, requests, responses = server
    examples = Path(__file__).resolve().parents[1] / 'examples'
    raw = yaml.safe_load((examples / 'cli-tools-sandboxed.yml').read_text())
    raw['profiles']['local'].update(base_url=url, features={'streaming': False})
    if not grant:
        raw['allowed_tools'] = []
    config = tmp_path / 'runtime.yml'
    config.write_text(yaml.safe_dump(raw))
    shutil.copytree(examples / '.prosaic', tmp_path / '.prosaic')
    executable = tmp_path / 'analyzer'
    executable.write_text(f'#!{sys.executable}\n' +
                          (examples / 'cli-tool/prosaic_example_analyzer.py').read_text())
    executable.chmod(0o700)
    manifest = tmp_path / '.prosaic/tools/analyze-spec.yml'
    data = yaml.safe_load(manifest.read_text())
    data['executable'] = str(executable)
    manifest.write_text(yaml.safe_dump(data))
    args = [sys.executable, str(examples / 'run_cli_tool.py'), '--config', str(config), '--profile', 'local']
    if live:
        responses.extend([completion('', [{'id': 'p', 'type': 'function', 'function': {
            'name': 'analyze_spec', 'arguments': '{"spec":"evidence/requirements.md"}'}}]), completion('checked')])
        args.append('--live')
    result = subprocess.run(args, cwd=tmp_path, capture_output=True, text=True, timeout=20)
    report = json.loads(result.stdout)
    assert result.returncode == (0 if grant else 1), result.stderr
    if live:
        assert report['tool_executed'] is True
        tool = next(m for m in requests[1]['messages'] if m['role'] == 'tool')
        assert json.loads(tool['content']) == {'status': 'ok', 'result': {
            'requirements': 2, 'vague_ids': ['REQ-002'], 'passed': False}}
    else:
        assert report['ok'] is grant
        assert requests == []
