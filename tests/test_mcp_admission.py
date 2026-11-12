
from prosaic_runtime import ProsaicRuntime, RunPolicy
from test_mcp_catalog import client, snapshot, spec
from support.conversation_server import ConversationFixture


def test_direct_mcp_tool_is_refused_without_network():
    host = client()
    tools = host.bind_tools(snapshot(host), [spec()])
    with ConversationFixture("openai-compatible") as fixture:
        runtime = ProsaicRuntime(fixture.runtime.config, custom_tools=tools)
    assert "mcp_client_v1" in runtime.capabilities
    result = runtime.run_tool("lookup", {"name": "k"},
                              policy=RunPolicy(allowed_tools=frozenset({"lookup"})))
    assert result.metadata["failure_reason"] == "unsupported_direct_tool"
