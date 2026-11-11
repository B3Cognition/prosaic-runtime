import hashlib
import json

import pytest

from prosaic_runtime import ToolResultContractV1, ToolResultV1
from prosaic_runtime.mcp import McpClientV1, McpServerV1, McpToolSpecV1, is_mcp_tool

SCHEMA = {"type": "object", "properties": {"name": {"type": "string"}},
          "required": ["name"], "additionalProperties": False}


def client(**overrides):
    callbacks = dict(allow_destination=lambda url: True, credentials=lambda *args: None,
                     scope_key=lambda context: "scope-1", authorize=lambda *args: True)
    callbacks.update(overrides)
    return McpClientV1(McpServerV1("catalog", "r1", "https://catalog.invalid/mcp",
                                  "2026-07-28", auth_mode="none"), **callbacks)


def spec(**overrides):
    fields = dict(alias="lookup", remote_name="find", description="Find a service",
                  binding_version="v1", result_mode="structured", read_only=True,
                  projector=lambda value, _: ToolResultV1(data=value),
                  result_contract=ToolResultContractV1("v1"))
    fields.update(overrides)
    return McpToolSpecV1(**fields)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def snapshot(host, schema=SCHEMA):
    document = {"version": 1, "binding": host._catalog_binding("scope-1"),
                "server": host._server_binding(),
                "tools": [{"name": "find", "inputSchema": schema, "outputSchema": None}]}
    return host.restore_catalog(document, access_scope="scope-1", expected_sha256=digest(document))


def test_offline_snapshot_roundtrip_and_alias_collision():
    host = client()
    original = snapshot(host)
    restored = host.restore_catalog(original.to_dict(), access_scope="scope-1",
                                    expected_sha256=original.sha256)
    tools = host.bind_tools(restored, [spec()])
    tool = tools["lookup"]
    assert is_mcp_tool(tool) and tool.with_context and not tool.parallel_read
    assert tool.version.startswith("mcp-v1.") and tool.parameters == SCHEMA
    assert "catalog.invalid" not in repr(tool.descriptor)
    with pytest.raises(ValueError, match="collision"):
        host.bind_tools(restored, [spec()], existing=tools)
    with pytest.raises(ValueError):
        host.restore_catalog(restored.to_dict(), access_scope="wrong",
                             expected_sha256=restored.sha256)
    with pytest.raises(ValueError):
        host.restore_catalog(restored.to_dict(), access_scope="scope-1",
                             expected_sha256="0" * 64)


@pytest.mark.parametrize("keyword,value", [("$ref", "https://other.invalid"), ("x-mcp-header", "Authorization"),
                                            ("pattern", ".+"), ("oneOf", [])])
def test_selected_schema_rejects_unsupported_keywords(keyword, value):
    host = client()
    catalog = snapshot(host, {**SCHEMA, keyword: value})
    with pytest.raises(ValueError):
        host.bind_tools(catalog, [spec()])


def test_open_remote_schema_requires_local_closed_schema():
    host = client()
    catalog = snapshot(host, {**SCHEMA, "additionalProperties": True})
    with pytest.raises(ValueError):
        host.bind_tools(catalog, [spec()])
    assert host.bind_tools(catalog, [spec(argument_schema=SCHEMA)])["lookup"].parameters == SCHEMA


def test_seal_tracks_policy_and_requires_read_only():
    host = client()
    catalog = snapshot(host)
    first = host.bind_tools(catalog, [spec()])["lookup"]
    changed = host.bind_tools(catalog, [spec(binding_version="v2")])["lookup"]
    assert first.version != changed.version
    with pytest.raises(ValueError):
        spec(read_only=False)


@pytest.mark.parametrize("endpoint", ["http://catalog.invalid/mcp", "https://a@catalog.invalid/mcp",
                                      "https://catalog.invalid/mcp?secret=x",
                                      "https://catalog.invalid/mcp#fragment"])
def test_destination_rejects_ambient_credentials_and_non_https(endpoint):
    with pytest.raises(ValueError):
        McpServerV1("catalog", "r1", endpoint, "2026-07-28")
