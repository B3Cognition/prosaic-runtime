"""Continuation data must survive the shared loop without reconstruction."""
from prosaic_runtime.openai_compatible import _OpenAICompletionTurn, _assistant_tool_message


def test_assistant_continuation_preserves_native_blocks():
    blocks = [{'type': 'text', 'text': 'checking', 'citations': []},
              {'type': 'tool_use', 'id': 't1', 'name': 'read_file', 'input': {'path': 'input'}}]
    turn = _OpenAICompletionTurn(
        text='checking', finish_reason='tool_calls', token_usage=7,
        token_usage_details={}, raw_response_metadata={}, reasoning_content_observed=False,
        tool_calls=[{'id': 't1', 'type': 'function', 'function': {
            'name': 'read_file', 'arguments': '{"path":"input"}'}}],
        streamed=False, http_status=200, raw_response_headers={}, continuation=blocks)
    assert _assistant_tool_message(turn)['provider_content'] == blocks
