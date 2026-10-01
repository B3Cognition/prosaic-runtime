"""Exercise the shipped CLI program and public runner with synthetic native calls."""
import json
from pathlib import Path
import subprocess
import sys
import yaml
from test_runtime import server, completion

EXAMPLES = Path(__file__).resolve().parents[1] / 'examples'
TOOL = EXAMPLES / 'cli-tool/prosaic_example_analyzer.py'


def test_example_cli_returns_deterministic_report_and_version():
    result = subprocess.run([sys.executable, str(TOOL), str(EXAMPLES / 'evidence/requirements.md'), '--json'],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'requirements': 2, 'vague_ids': ['REQ-002'], 'passed': False}
    version = subprocess.run([sys.executable, str(TOOL), '--version'], capture_output=True, text=True, timeout=5)
    assert version.returncode == 0 and '1.0' in version.stdout


def test_example_preflight_and_live_runner_use_the_cli(server, tmp_path):
    url, requests, responses = server
    wrapper = tmp_path / 'analyzer'
    wrapper.write_text(f'#!{sys.executable}\nimport runpy\nrunpy.run_path({str(TOOL)!r}, run_name="__main__")\n')
    wrapper.chmod(0o755)
    tools = tmp_path / 'tools'; tools.mkdir()
    data = yaml.safe_load((EXAMPLES / '.prosaic/tools/analyze-spec.yml').read_text())
    data['executable'] = str(wrapper)
    (tools / 'analyze.yml').write_text(yaml.safe_dump(data))
    config = yaml.safe_load((EXAMPLES / 'cli-tools.yml').read_text())
    config['tool_directories'] = [str(tools)]
    for endpoint in config['profiles'].values():
        endpoint.update(base_url=url, api_key_env=None, features={'streaming': False})
    path = tmp_path / 'runtime.yml'; path.write_text(yaml.safe_dump(config))
    command = [sys.executable, str(EXAMPLES / 'run_cli_tool.py'), '--config', str(path)]
    preflight = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert preflight.returncode == 0, preflight.stderr
    assert json.loads(preflight.stdout)['ok'] is True and requests == []
    responses.extend([completion('', [{'id': 'analyze', 'type': 'function', 'function': {
        'name': 'analyze_spec', 'arguments': '{"spec":"evidence/requirements.md"}'}}]),
        completion('Two requirements; REQ-002 is vague.')])
    result = subprocess.run([*command, '--live'], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report['result']['exit_code'] == 0 and report['tool_executed'] is True
    tool_message = next(m for m in requests[1]['messages'] if m['role'] == 'tool')
    assert json.loads(tool_message['content'])['result']['vague_ids'] == ['REQ-002']
