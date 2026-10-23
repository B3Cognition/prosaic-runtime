import json

import pytest
import yaml

from prosaic_runtime.cli import main
from test_prework_safety import rejection_server
from test_runtime import completion


BASE = ['--timeout', '10', '--max-attempts', '3', '--max-native-attempts', '0']
API = ['--enable-api', '--api-timeout', '5', '--max-api-requests', '2',
       '--max-api-tool-calls', '0', '--max-tool-rounds', '2']


def setup(tmp_path, url):
    source = tmp_path / '.prosaic' / 'subagents'
    source.mkdir(parents=True)
    (source / 'task.md').write_text('---\nname: task\ndescription: Test\nexecution: agent\ntools: none\n---\nAnswer.')
    (tmp_path / 'api.yml').write_text(yaml.safe_dump({'profiles': {'local': {
        'base_url': url, 'model': 'fixture', 'features': {'streaming': False}}},
        'default_profile': 'local'}))
    config = tmp_path / 'unified.yml'
    config.write_text(yaml.safe_dump({'kind': 'unified', 'api_config': 'api.yml',
        'api_quota_keys': {'local': 'service'}, 'pools': {'agents': {'profiles': ['local']}},
        'default_pool': 'agents'}))
    return ['subagents/task.md', '--config', str(config), '--source', str(source.parent),
            '--cwd', str(tmp_path), '--quiet']


@pytest.mark.parametrize('missing', ['--timeout', '--max-attempts', '--max-native-attempts',
    '--api-timeout', '--max-api-requests', '--max-api-tool-calls', '--max-tool-rounds'])
def test_missing_budget_rejected_before_dispatch(tmp_path, capsys, missing):
    args = setup(tmp_path, 'http://127.0.0.1:9/v1') + BASE + API
    index = args.index(missing)
    del args[index:index + 2]
    assert main(args) == 2
    assert missing in capsys.readouterr().err


@pytest.mark.parametrize('extra', [[], ['--api-timeout', '5'], ['--allow-tool', 'read_file'],
    ['--native-timeout', '5'], ['--profile', 'local'], ['--accounting-provider', 'anthropic']])
def test_disabled_backends_and_incompatible_options_rejected(tmp_path, capsys, extra):
    assert main(setup(tmp_path, 'http://127.0.0.1:9/v1') + BASE + extra) == 2
    assert capsys.readouterr().err


@pytest.mark.parametrize('output', [[], ['--output', 'text'], ['--events']])
def test_actual_api_execution_and_output(tmp_path, capsys, rejection_server, output):
    url, requests, responses = rejection_server
    responses.append((200, completion('done')[1], 0))
    assert main(setup(tmp_path, url) + BASE + API + output) == 0
    captured = capsys.readouterr()
    assert len(requests) == 1 and captured.err == ''
    if output == ['--output', 'text']:
        assert captured.out == 'done\n'
    else:
        events = [json.loads(line) for line in captured.out.splitlines()]
        assert events[-1]['event'] == 'result'
        assert events[-1]['metadata']['unified_execution_v1']['api_provider_requests'] == 1
        assert len(events) > 1 if output else len(events) == 1


def test_zero_request_cap_dispatches_nothing(tmp_path, capsys, rejection_server):
    url, requests, _ = rejection_server
    args = setup(tmp_path, url) + BASE + API
    args[args.index('--max-api-requests') + 1] = '0'
    assert main(args) == 1
    assert requests == []
    assert json.loads(capsys.readouterr().out)['metadata']['unified_execution_v1']['api_provider_requests'] == 0


@pytest.mark.parametrize('extra', [['--max-attempts', '2'], ['--enable-api'],
    ['--max-api-requests', '0'], ['--require-capability', 'file_tools']])
def test_unified_flags_cannot_be_silently_ignored_in_legacy_mode(tmp_path, capsys, extra):
    path = tmp_path / 'api.yml'
    path.write_text('profiles: {}\n')
    assert main(['task', '--config', str(path)] + extra) == 2
    assert 'kind: unified' in capsys.readouterr().err


@pytest.mark.parametrize('flag,value', [('--timeout', 'nan'), ('--max-attempts', '0'),
    ('--max-native-attempts', '-1'), ('--api-timeout', 'inf'), ('--max-api-requests', '-1'),
    ('--max-api-tool-calls', '-1'), ('--max-tool-rounds', '0'), ('--max-api-reported-tokens', '-1')])
def test_invalid_budget_rejected(tmp_path, capsys, flag, value):
    args = setup(tmp_path, 'http://127.0.0.1:9/v1') + BASE + API
    args += [flag, value]
    assert main(args) == 2
    assert capsys.readouterr().err
