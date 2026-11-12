# Runtime 0.9.22 — MCP client

Install `b3-prosaic-runtime[mcp]==0.9.22` to register host-approved MCP read tools.
The client supports Streamable HTTP profiles 2026-07-28 and 2025-11-25, host-owned
endpoint approval and resource-bound credentials, and bounded catalog discovery.

Register the aliases that an agent may use and supply the host journal and current
authorization callback. Each journaled call owns one dispatch slot. Replay checks
current authorization again and can use saved results with the MCP endpoint offline.
MCP calls are serial barriers within parallel tool batches.

The extra is optional for existing consumers. The release retains Core 0.5.0 and
the published PostgreSQL recorder 0.3.0. See the [README](../README.md#mcp-read-tools)
for catalog and binding setup.
