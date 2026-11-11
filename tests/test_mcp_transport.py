from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread

import pytest

from prosaic_runtime.mcp import McpClientV1, McpLimitsV1, McpServerV1
from test_mcp_catalog import SCHEMA

pytest.importorskip("mcp")


@contextmanager
def endpoint(answer):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.command, self.path, request, dict(self.headers)))
            status, headers, result = answer(request)
            self.send_response(status)
            if type(result) is not bytes and result is not None:
                result = json.dumps({"jsonrpc": "2.0", "id": request.get("id"), "result": result}).encode()
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            if result is not None:
                self.wfile.write(result)

        def do_GET(self):
            requests.append((self.command, self.path, None, dict(self.headers)))
            self.send_error(405)

        def do_DELETE(self):
            requests.append((self.command, self.path, None, dict(self.headers)))
            self.send_response(204)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp", requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)


def opened(url, version="2026-07-28", **overrides):
    callbacks = dict(allow_destination=lambda endpoint: endpoint == url,
                     credentials=lambda *_: pytest.fail("credential-free scope"),
                     scope_key=lambda _: "scope-1", authorize=lambda *_: True)
    callbacks.update(overrides)
    return McpClientV1(McpServerV1("service", "r1", url, version, auth_mode="none",
                                  allow_loopback_http=True), **callbacks)


def listing(*, cursor=None, version="2026-07-28"):
    result = {"tools": [{"name": "find", "inputSchema": SCHEMA}]}
    if version == "2026-07-28":
        result.update(cacheScope="private", resultType="complete", ttlMs=0)
    if cursor:
        result["nextCursor"] = cursor
    return 200, {"Content-Type": "application/json"}, result


@pytest.mark.parametrize("version", ["2026-07-28", "2025-11-25"])
def test_profile_discovery_with_sdk(version):
    def answer(request):
        method = request["method"]
        if method == "initialize":
            return 200, {"Content-Type": "application/json", "Mcp-Session-Id": "test"}, {
                "protocolVersion": version, "serverInfo": {"name": "fixture", "version": "1"},
                "capabilities": {"tools": {}},
            }
        if method == "notifications/initialized":
            return 202, {}, None
        assert method == "tools/list"
        return listing(version=version)

    with endpoint(answer) as (url, requests), opened(url, version) as host:
        result = host.discover(access_scope="scope-1")
        assert result.to_dict()["tools"][0]["name"] == "find"
    methods = [item[2]["method"] for item in requests if item[0] == "POST"]
    assert methods == (["tools/list"] if version == "2026-07-28"
                       else ["initialize", "notifications/initialized", "tools/list"])
    assert all(item[0] != "GET" for item in requests)
    assert sum(item[0] == "DELETE" for item in requests) == (version == "2025-11-25")


@pytest.mark.parametrize("reply", [
    lambda _: listing(cursor="repeat"),
    lambda _: (307, {"Location": "/redirect"}, None),
    lambda _: (200, {"Content-Type": "application/json"}, b"x" * 131073),
    lambda _: (200, {"Content-Type": "text/event-stream"}, b": heartbeat\n\n" * 65),
    lambda _: (200, {"Content-Type": "application/json", "Content-Encoding": "gzip"}, b"x"),
])
def test_refuse_malformed_or_unbounded_discovery(reply):
    with endpoint(reply) as (url, requests), opened(
            url, limits=McpLimitsV1(discovery_timeout_s=1)) as host:
        with pytest.raises(ValueError, match="MCP discovery failed"):
            host.discover(access_scope="scope-1")
    assert requests
    assert all(method == "POST" and path == "/mcp" for method, path, *_ in requests)
