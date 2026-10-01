import importlib.util
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import pytest
import yaml
from test_runtime import server
from test_acquisition import reply
from test_custom_tool_transport import call

EXAMPLES = Path(__file__).resolve().parents[1] / 'examples'
RECORD = {'sku': 'SKU-001', 'name': 'Demo Widget', 'price_cents': 1250, 'currency': 'USD'}


def invoke(url, tmp_path, *args, grant=True, streaming=False):
    if not shutil.which('prosaic'):
        pytest.skip('install Prosaic CLI for example integration tests')
    config = tmp_path / 'runtime.yml'
    config.write_text(yaml.safe_dump({'default_profile': 'local', 'routes': {'fast': 'local'},
        'allowed_tools': ['lookup_catalog'] if grant else [],
        'profiles': {'local': {'base_url': url, 'model': 'test', 'features': {'streaming': streaming}}}}))
    env = dict(os.environ)
    return subprocess.run([sys.executable, str(EXAMPLES / 'run_custom_tool.py'), '--config', str(config), *args],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=20)


@pytest.mark.parametrize('streaming', [False, True])
def test_shipped_example_native_lookup(server, tmp_path, streaming):
    url, requests, responses = server
    answer = {'found': True, 'item': RECORD}
    responses.extend([reply('', [call()], streaming), reply(json.dumps(answer), streaming=streaming)])
    result = invoke(url, tmp_path, '--live', streaming=streaming)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert json.loads(output['result']['stdout']) == answer
    completed = next(e for e in output['events'] if e['event'] == 'tool_completed')
    assert completed['status'] == 'ok' and completed['tool_version'].startswith('catalog-')
    assert json.loads(next(m for m in requests[1]['messages'] if m['role'] == 'tool')['content']) == {'status': 'ok', 'result': answer}


def test_example_requires_live_and_grants(server, tmp_path):
    url, requests, responses = server
    assert invoke(url, tmp_path).returncode != 0
    assert invoke(url, tmp_path, '--live', grant=False).returncode != 0
    assert requests == []


@pytest.mark.parametrize('sku,tool_value,error', [
    ('SKU-999', {'found': False, 'item': None}, None), ('invalid', None, 'invalid_arguments')])
def test_not_found_and_invalid_sku(server, tmp_path, sku, tool_value, error):
    url, requests, responses = server
    responses.extend([reply('', [call(json.dumps({'sku': sku}))]), reply('done')])
    result = invoke(url, tmp_path, '--live', '--sku', sku)
    assert result.returncode == 0, result.stderr
    payload = json.loads(next(m for m in requests[1]['messages'] if m['role'] == 'tool')['content'])
    assert payload == ({'status': 'error', 'error': error} if error else {'status': 'ok', 'result': tool_value})


def test_catalogue_module_fixed_contract():
    spec = importlib.util.spec_from_file_location('runtime_example_catalog', EXAMPLES / 'catalog_tools.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    tool = module.make_tools()['lookup_catalog']
    assert tool.handler({'sku': 'SKU-001'}) == {'found': True, 'item': RECORD}
    assert tool.parameters == {'type': 'object', 'required': ['sku'], 'properties': {
        'sku': {'type': 'string', 'pattern': '^SKU-[0-9]{3}$'}}, 'additionalProperties': False}
    assert tool.descriptor['max_argument_bytes'] == 16384 and tool.descriptor['max_result_bytes'] == 65536
    assert tool.descriptor['authorization_required'] is False
