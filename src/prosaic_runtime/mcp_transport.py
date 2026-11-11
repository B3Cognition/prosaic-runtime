"""Bounded Streamable HTTP I/O for the optional MCP SDK."""

from contextlib import contextmanager
from contextvars import ContextVar
import json
import logging
import re
from threading import Event, Lock, get_ident
from time import monotonic

import anyio
from anyio.from_thread import start_blocking_portal
import httpx2
from jsonschema import Draft202012Validator, ValidationError
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent

from .events import Cancelled
from .mcp_catalog import LEGACY, McpCatalogV1, admitted_schema, canonical, strip_schema_prose
from .operation_context import bounded_reference
from .tools import ToolDeadlineExceeded, reject_constant
from .tool_contracts import ToolResultV1
from .tool_effects import ToolFailure


_PRIVATE_IO = ContextVar("mcp_private_io", default=False)
_LOG_LOCK = Lock()
_LOG_USERS = 0
_LOGGERS = []


def _filter_record(_record):
    return not _PRIVATE_IO.get()


@contextmanager
def _private_sdk_io():
    # The upstream SDK can log raw frames and exceptions at debug level.
    # Filter only records emitted by this adapter's operation context.
    global _LOG_USERS
    with _LOG_LOCK:
        for name, logger in list(logging.root.manager.loggerDict.items()):
            if (isinstance(logger, logging.Logger)
                    and name.split(".")[0] in {"mcp", "httpx2", "httpcore2"}
                    and logger not in _LOGGERS):
                logger.addFilter(_filter_record)
                _LOGGERS.append(logger)
        _LOG_USERS += 1
    token = _PRIVATE_IO.set(True)
    try:
        yield
    finally:
        _PRIVATE_IO.reset(token)
        with _LOG_LOCK:
            _LOG_USERS -= 1
            if not _LOG_USERS:
                for logger in _LOGGERS:
                    logger.removeFilter(_filter_record)
                _LOGGERS.clear()


class _ProtocolError(httpx2.TransportError):
    def __init__(self):
        super().__init__("MCP transport rejected a frame")


def _unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise _ProtocolError()
        value[key] = item
    return value


def _response_object(raw, request_id, *, notifications=False):
    try:
        frame = json.loads(raw, object_pairs_hook=_unique_pairs, parse_constant=reject_constant)
    except (ValueError, RecursionError, UnicodeError):
        raise _ProtocolError() from None
    if type(frame) is not dict or frame.get("jsonrpc") != "2.0":
        raise _ProtocolError()
    if notifications and "method" in frame and "id" not in frame:
        return None
    if ("method" in frame or "id" not in frame or type(frame["id"]) is not type(request_id)
            or frame["id"] != request_id or ("result" in frame) == ("error" in frame)):
        raise _ProtocolError()
    return frame


class _BoundedStream(httpx2.AsyncByteStream):
    def __init__(self, stream, transport, request_id, *, sse):
        self.stream, self.transport, self.request_id, self.sse = stream, transport, request_id, sse

    async def __aiter__(self):
        if not self.sse:
            parts = bytearray()
            async for chunk in self.stream:
                self.transport.consume(len(chunk))
                parts.extend(chunk)
            _response_object(parts, self.request_id)
            yield bytes(parts)
            return
        event = bytearray()
        line = bytearray()
        after_cr = False
        async for chunk in self.stream:
            self.transport.consume(len(chunk))
            for byte in chunk:
                if after_cr and byte == 10:
                    after_cr = False
                    continue
                after_cr = byte == 13
                if byte not in (10, 13):
                    line.append(byte)
                elif line:
                    event.extend(line)
                    event.append(10)
                    line.clear()
                elif event:
                    self.transport.sse_events += 1
                    if self.transport.sse_events > self.transport.limits.max_sse_events:
                        raise _ProtocolError()
                    data = [item[5:].removeprefix(b" ") for item in event.split(b"\n")
                            if item.startswith(b"data:")]
                    event.clear()
                    if data and any(data):
                        frame = _response_object(b"\n".join(data), self.request_id, notifications=True)
                        if frame is not None:
                            # Only a matching terminal frame reaches the SDK.
                            # Strip server resumption tokens and retry directives.
                            yield b"data: " + json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode() + b"\n\n"
                            return
                if len(line) + len(event) > self.transport.limits.max_sse_event_bytes:
                    raise _ProtocolError()

    async def aclose(self):
        await self.stream.aclose()


class _Operation:
    def __init__(self, tool_name, arguments=None):
        self.name, self.arguments = tool_name, arguments
        self.owner = get_ident()
        self.lock = Lock()
        self.ready, self.permit, self.stop = Event(), Event(), Event()
        self.finished, self.released = Event(), Event()
        self.sent = False
        self.result = None
        self.failure = "mcp_unavailable"

    def begin_send(self, name, arguments):
        with self.lock:
            if (self.sent or self.stop.is_set() or not self.permit.is_set()
                    or self.name is None or name != self.name
                    or canonical(arguments) != self.arguments):
                raise _ProtocolError()
            self.sent = True

    def cancel(self):
        with self.lock:
            if self.result is None:
                self.stop.set()

    def finish(self, result):
        with self.lock:
            if not self.stop.is_set():
                self.result = result


class _Transport(httpx2.AsyncBaseTransport):
    def __init__(self, server, limits, operation):
        self.url = httpx2.URL(server.endpoint)
        if str(self.url) != server.endpoint:
            raise _ProtocolError()
        self.legacy = server.protocol_version == LEGACY
        self.limits, self.operation = limits, operation
        self.transport = httpx2.AsyncHTTPTransport(retries=0, trust_env=False)
        self.bytes = self.sse_events = 0
        self.session_id = None
        self.delete_sent = False

    def consume(self, size):
        self.bytes += size
        if self.bytes > self.limits.max_transport_bytes:
            raise _ProtocolError()

    async def handle_async_request(self, request):
        if request.url != self.url or request.url.raw_path != self.url.raw_path:
            raise _ProtocolError()
        if request.method == "GET":
            return httpx2.Response(405)
        request.headers.pop("cookie", None)
        request_id = None
        if request.method == "DELETE":
            if not self.legacy or self.session_id is None or self.delete_sent:
                raise _ProtocolError()
            self.delete_sent = True
        elif request.method == "POST":
            try:
                payload = json.loads(request.content)
            except (ValueError, UnicodeError):
                raise _ProtocolError() from None
            method, request_id = payload.get("method"), payload.get("id")
            if method == "tools/call":
                params = payload["params"]
                self.operation.begin_send(params["name"], params.get("arguments", {}))
            elif self.operation.stop.is_set():
                raise _ProtocolError()
            elif method != "tools/list" and not (self.legacy and method in (
                    "initialize", "notifications/initialized")):
                # No reflected server RPC response or other SDK background traffic.
                raise _ProtocolError()
        else:
            raise _ProtocolError()
        response = await self.transport.handle_async_request(request)
        try:
            if 300 <= response.status_code < 400:
                raise _ProtocolError()
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise _ProtocolError()
            session = response.headers.get("mcp-session-id")
            if self.legacy and session is not None:
                if not bounded_reference(session):
                    raise _ProtocolError()
                self.session_id = session
            # Bound even error bodies, which the SDK's HTTP path may read.
            if 200 <= response.status_code < 300 and request_id is not None:
                mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if mime not in ("application/json", "text/event-stream"):
                    raise _ProtocolError()
                response.stream = _BoundedStream(response.stream, self, request_id,
                                                  sse=mime == "text/event-stream")
            else:
                await response.aclose()
                return httpx2.Response(response.status_code)
            return response
        except BaseException:
            await response.aclose()
            raise

    async def aclose(self):
        await self.transport.aclose()


class _Lifecycle:
    def __init__(self):
        self.manager = start_blocking_portal(name="prosaic-mcp")
        self.portal = self.manager.__enter__()
        self.operations = set()
        self.closing = False
        self.closed = Event()


def open_client(client):
    with client._lock:
        if client._lifecycle is not None:
            raise ValueError("MCP client already open")
        client._lifecycle = _Lifecycle()


def close_client(client):
    with client._lock:
        life = client._lifecycle
        if life is None:
            return
        if any(op.owner == get_ident() for op in life.operations):
            raise ValueError("cannot close MCP client from an active callback")
        closing = life.closing
        life.closing = True
        operations = tuple(life.operations)
    if closing:
        life.closed.wait()
        return
    try:
        for operation in operations:
            operation.cancel()
        for operation in operations:
            operation.released.wait()
        life.manager.__exit__(None, None, None)
    finally:
        with client._lock:
            client._lifecycle = None
        life.closed.set()


@contextmanager
def _reserve(client, operation):
    with client._lock:
        life = client._lifecycle
        if life is None or life.closing or len(life.operations) >= client.limits.max_inflight:
            life = None
        else:
            life.operations.add(operation)
    try:
        yield life
    finally:
        if life is not None:
            with client._lock:
                life.operations.remove(operation)
            operation.released.set()


def _credentials(client, scope):
    if client._allow_destination(client.server.endpoint) is not True:
        raise ValueError("MCP destination denied")
    if client.server.auth_mode == "none":
        return {}
    token = client._credentials(client.server.server_id, scope)
    if type(token) is not str or re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,8192}", token) is None:
        raise ValueError("invalid MCP resource credential")
    return {"Authorization": "Bearer " + token}


async def _list(sdk, limits):
    entries = {}
    cursors = set()
    cursor = None
    for _ in range(limits.max_list_pages):
        page = await sdk.list_tools(cursor=cursor, cache_mode="bypass")
        for tool in page.tools:
            if not bounded_reference(tool.name) or tool.name in entries:
                raise ValueError("invalid MCP catalogue entry")
            entries[tool.name] = {"name": tool.name, "inputSchema": strip_schema_prose(tool.input_schema),
                                  "outputSchema": strip_schema_prose(tool.output_schema)}
            if len(entries) > limits.max_catalog_tools:
                raise ValueError("MCP catalogue tool limit")
        canonical(list(entries.values()), limits.max_catalog_bytes)
        cursor = page.next_cursor
        if cursor is None:
            return entries
        if not bounded_reference(cursor, 1024) or cursor in cursors:
            raise ValueError("invalid MCP catalogue cursor")
        cursors.add(cursor)
    raise ValueError("MCP catalogue page limit")


async def _monitor_stop(operation, scope):
    while not operation.stop.is_set():
        await anyio.sleep(0.01)
    scope.cancel()


async def _worker(client, operation, timeout, headers, handler, arguments):
    try:
        with _private_sdk_io():
            await _run_io(client, operation, timeout, headers, handler, arguments)
    except Exception:
        # No SDK or transport exception escapes to a host-facing diagnostic.
        pass
    finally:
        operation.finished.set()


async def _run_io(client, operation, timeout, headers, handler, arguments):
    if operation.stop.is_set():
        return

    transport = _Transport(client.server, client.limits, operation)
    http = httpx2.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                              headers={**headers, "Accept-Encoding": "identity"}, timeout=timeout)
    try:
        with anyio.fail_after(timeout):
            async with anyio.create_task_group() as group:
                group.start_soon(_monitor_stop, operation, group.cancel_scope)
                try:
                    stream = streamable_http_client(client.server.endpoint, http_client=http,
                                                   terminate_on_close=False,
                                                   max_sse_event_size=client.limits.max_sse_event_bytes)
                    mode = "legacy" if client.server.protocol_version == LEGACY else client.server.protocol_version
                    async with Client(stream, mode=mode, read_timeout_seconds=timeout) as sdk:
                        if sdk.protocol_version != client.server.protocol_version:
                            raise ValueError("MCP negotiated profile differs")
                        entries = await _list(sdk, client.limits)
                        if handler is None:
                            operation.finish(entries)
                        else:
                            await _call(sdk, operation, client.limits, handler, entries, arguments)
                finally:
                    group.cancel_scope.cancel()
    finally:
        with anyio.CancelScope(shield=True):
            if transport.session_id is not None:
                with anyio.move_on_after(client.limits.cleanup_timeout_s):
                    try:
                        await http.delete(client.server.endpoint, headers={
                            "Mcp-Session-Id": transport.session_id, "MCP-Protocol-Version": LEGACY,
                        })
                    except Exception:
                        pass
            await http.aclose()

async def _call(sdk, operation, limits, handler, entries, arguments):
    entry = entries.get(operation.name)
    operation.failure = "mcp_catalogue_changed"
    if entry is None:
        return
    current = {
        "inputSchema": admitted_schema(entry["inputSchema"], limits.max_schema_bytes, input_schema=True),
        "outputSchema": admitted_schema(entry["outputSchema"], limits.max_schema_bytes)
                        if entry["outputSchema"] is not None else None,
    }
    if canonical(current) != handler.schemas:
        return
    try:
        Draft202012Validator(current["inputSchema"]).validate(arguments)
    except ValidationError:
        operation.failure = "mcp_invalid_arguments"
        return
    operation.failure = "mcp_authorization_denied"
    operation.ready.set()
    while not operation.permit.is_set():
        await anyio.sleep(0.005)
    result = await sdk.session.call_tool(operation.name, arguments, allow_input_required=False,
                                         allow_claimed=False)
    if type(result) is not CallToolResult or result.is_error:
        return
    if handler.spec.result_mode == "structured":
        value = result.structured_content
        if type(value) is not dict:
            return
        if current["outputSchema"] is not None:
            Draft202012Validator(current["outputSchema"]).validate(value)
    else:
        if (result.structured_content is not None or not 1 <= len(result.content) <= 16
                or any(type(block) is not TextContent for block in result.content)):
            return
        value = {"text": [block.text for block in result.content]}
    operation.finish(canonical(value, limits.max_result_bytes))


def discover(client, access_scope):
    binding = client._catalog_binding(access_scope)
    operation = _Operation(None)
    with _reserve(client, operation) as life:
        try:
            if life is None:
                raise ValueError
            headers = _credentials(client, access_scope)
            life.portal.start_task_soon(_worker, client, operation, client.limits.discovery_timeout_s,
                                       headers, None, None)
            operation.finished.wait()
            if operation.result is None:
                raise ValueError
            entries = operation.result
            document = {"version": 1, "binding": binding, "server": client._server_binding(),
                        "tools": [entries[name] for name in sorted(entries)]}
            return McpCatalogV1(document, limits=client.limits, binding=binding,
                                server=client._server_binding())
        except Exception:
            raise ValueError("MCP discovery failed") from None


def dispatch(handler, arguments, context):
    client = handler.client
    operation = _Operation(handler.spec.remote_name, canonical(arguments, client.limits.max_argument_bytes))
    if not handler.authorize(arguments, context):
        return ToolFailure("mcp_authorization_denied")
    with _reserve(client, operation) as life:
        if life is None:
            return ToolFailure("mcp_client_busy")
        try:
            scope = client._scope_key(context)
            if client._catalog_binding(scope) != handler.binding:
                return ToolFailure("mcp_authorization_denied")
            headers = _credentials(client, scope)
        except Exception:
            return ToolFailure("mcp_authorization_denied")
        try:
            timeout = context.remaining_timeout(client.limits.call_timeout_s)
        except (Cancelled, ToolDeadlineExceeded):
            return ToolFailure("mcp_unavailable")
        deadline = monotonic() + timeout
        life.portal.start_task_soon(_worker, client, operation, timeout, headers, handler, arguments)
        try:
            while not operation.finished.wait(0.01):
                if context.cancelled() or monotonic() >= deadline:
                    operation.cancel()
                if operation.ready.is_set() and not operation.permit.is_set() and not operation.stop.is_set():
                    try:
                        permitted = (handler.authorize(arguments, context)
                                     and client._allow_destination(client.server.endpoint) is True)
                    except Exception:
                        permitted = False
                    if permitted and not context.cancelled() and monotonic() < deadline:
                        operation.permit.set()
                    else:
                        operation.cancel()
        finally:
            operation.cancel()
            operation.finished.wait()
        if operation.result is not None:
            value = handler.spec.projector(json.loads(operation.result), context)
            if type(value) is not ToolResultV1:
                raise RuntimeError("MCP result unavailable")
            return value
        if operation.sent:
            raise RuntimeError("MCP result unavailable")
        return ToolFailure(operation.failure)
