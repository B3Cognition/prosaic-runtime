"""oMLX grammar requests constrain generation, never grant tool authority."""
import pytest
from prosaic_runtime import EndpointConfig
from test_runtime import server, artifact, runtime, completion
from test_acquisition import read, reply, policy
from test_anthropic import native_server, native_runtime


@pytest.mark.parametrize('streaming', [False, True])
def test_envelope_constraint_only_applies_to_native_tool_phase(server, tmp_path, streaming):
    url, requests, responses = server
    (tmp_path/'input').write_text('evidence')
    responses.extend([reply('', read(), streaming), reply('final answer', streaming=streaming)])
    result = runtime(EndpointConfig(url, 'fixture', features={
        'streaming': streaming, 'omlx_tool_call_envelope': True,
        'finalize_after_tool_rounds': 1, 'json_mode': True})).run(
            artifact('read'), cwd=tmp_path, policy=policy(max_tool_rounds=1))
    assert result.exit_code == 0 and result.stdout == 'final answer'
    assert requests[0]['structured_outputs'] == {'regex': r'<tool_call>\s*\{[\s\S]*\}\s*</tool_call>'}
    assert 'response_format' not in requests[0]
    assert 'structured_outputs' not in requests[1] and 'tools' not in requests[1]
    assert requests[1]['response_format'] == {'type': 'json_object'}
    assert result.token_usage == 14


@pytest.mark.parametrize('tools', [False, True])
@pytest.mark.parametrize('features', [
    {'omlx_tool_call_envelope': 'true', 'finalize_after_tool_rounds': 1},
    {'omlx_tool_call_envelope': 1, 'finalize_after_tool_rounds': 1},
    {'omlx_tool_call_envelope': None, 'finalize_after_tool_rounds': 1},
    {'omlx_tool_call_envelope': True},
])
def test_invalid_envelope_controls_fail_before_http(server, features, tools):
    url, requests, responses = server
    responses.append(completion('must not run'))
    with pytest.raises(ValueError, match='omlx_tool_call_envelope'):
        runtime(EndpointConfig(url, 'fixture', features=features)).run(
            artifact('read' if tools else ''), policy=policy() if tools else None)
    assert requests == []


def test_anthropic_rejects_vendor_constraint_before_http(native_server):
    url, requests, responses = native_server
    with pytest.raises(ValueError, match='unsupported.*omlx_tool_call_envelope'):
        native_runtime(url, features={'omlx_tool_call_envelope': True,
            'finalize_after_tool_rounds': 1}).run(artifact('read'), policy=policy())
    assert requests == []


def test_ignored_envelope_never_promotes_text_to_a_tool_call(server, tmp_path):
    url, requests, responses = server
    responses.append(completion('```json\n{"name":"read_file","arguments":{"path":"input"}}\n```'))
    events = []
    result = runtime(EndpointConfig(url, 'fixture', features={
        'streaming': False, 'omlx_tool_call_envelope': True,
        'finalize_after_tool_rounds': 1})).run(artifact('read'), cwd=tmp_path,
            policy=policy(), on_event=events.append)
    assert result.metadata['provider_error_code'] == 'tool_choice_not_honored'
    assert not any(event['event']=='tool_started' for event in events)
    assert len(requests) == 1 and result.token_usage == 7
