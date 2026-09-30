"""Unknown or invalid endpoint usage must not bypass consumer token budgets."""
import json
import pytest
from prosaic_runtime import EndpointConfig, RunPolicy
from test_runtime import server, artifact, runtime, completion
from test_acquisition import reply, read


@pytest.mark.parametrize('streaming', [False, True])
@pytest.mark.parametrize('usage,want', [(None, None), ({}, None), ({'prompt_tokens': 2}, None),
    ({'total_tokens': -1}, None), ({'total_tokens': True}, None),
    ({'total_tokens': 0}, 0), ({'prompt_tokens': 2, 'completion_tokens': 3}, 5)])
def test_public_usage_is_known_only_when_reported_complete_and_nonnegative(server, usage, want, streaming):
    url, _, responses = server
    if streaming:
        chunks = [{'choices': [{'delta': {'content': 'done'}, 'finish_reason': 'stop'}]},
                  {'choices': [], **({'usage': usage} if usage is not None else {})}]
        responses.append(('text/event-stream', (''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks) + 'data: [DONE]\n\n').encode()))
    else:
        _, body = completion()
        parsed = json.loads(body)
        parsed.pop('usage')
        if usage is not None:
            parsed['usage'] = usage
        responses.append(('application/json', json.dumps(parsed).encode()))
    result = runtime(EndpointConfig(url, 'test', features={'streaming': streaming})).run(artifact())
    assert result.exit_code == 0
    assert result.token_usage == want
    assert result.metadata['token_usage_status'] == ('unknown' if want is None else 'reported')


@pytest.mark.parametrize('missing_turn', [0, 1])
def test_one_missing_usage_turn_does_not_become_a_known_tool_loop_total(server, tmp_path, missing_turn):
    url, _, responses = server
    (tmp_path / 'input').write_text('evidence')
    for i, item in enumerate([completion('', read()), completion()]):
        kind, body = item
        if i == missing_turn:
            parsed = json.loads(body)
            parsed.pop('usage')
            body = json.dumps(parsed).encode()
        responses.append((kind, body))
    result = runtime(EndpointConfig(url, 'test', features={'streaming': False})).run(
        artifact('read'), acquisition=artifact('read'), cwd=tmp_path,
        policy=RunPolicy(allowed_tools=frozenset({'read_file'}), read_roots=('.',), initial_tool='read_file'))
    assert result.exit_code == 0
    assert result.token_usage is None
    assert result.metadata['reported_token_usage'] == 7
