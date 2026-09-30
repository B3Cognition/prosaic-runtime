"""A no-tool invocation must obey the same serialized-request bound as tools."""
from prosaic_runtime import EndpointConfig, RunPolicy
from test_runtime import server, completion, artifact, runtime


def test_no_tool_request_overhead_cannot_bypass_input_limit(server):
    url, requests, responses = server
    responses.append(completion())
    result = runtime(EndpointConfig(url, 'test', features={'streaming': False})).run(
        artifact(), policy=RunPolicy(max_input_bytes=64))
    assert result.exit_code == 1 and result.metadata['failure_reason'] == 'budget_exceeded'
    assert requests == []
