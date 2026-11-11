"""Optional host-owned MCP client and ordinary Runtime tool registrations."""

from copy import deepcopy
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
import json
import re
from threading import Lock

from .mcp_catalog import LEGACY, MODERN, McpCatalogV1, McpLimitsV1, McpServerV1, admitted_schema, canonical, digest
from .operation_context import bounded_reference
from .tool_contracts import ToolContractBindingsV1, ToolEventPolicyV1, ToolResultContractV1
from .tools import CustomTool, validate_custom_tools

__all__ = ["McpCatalogV1", "McpClientV1", "McpLimitsV1", "McpServerV1",
           "McpToolSpecV1", "is_mcp_tool", "require_mcp_sdk"]


def require_mcp_sdk():
    try:
        installed = version("mcp")
    except PackageNotFoundError:
        raise ValueError("MCP requires b3-prosaic-runtime[mcp]") from None
    match = re.fullmatch(r"2\.(\d+)\.\d+(?:\.post\d+)?(?:\+[a-zA-Z0-9.]+)?", installed)
    if match is None or int(match[1]) < 3:
        raise ValueError("unsupported MCP SDK version")


@dataclass(frozen=True)
class McpToolSpecV1:
    alias: str
    remote_name: str
    description: str
    binding_version: str
    result_mode: str
    projector: object
    result_contract: ToolResultContractV1
    read_only: bool = field(kw_only=True)
    argument_schema: dict | None = None
    event_policy: ToolEventPolicyV1 | None = None

    def __post_init__(self):
        if self.read_only is not True or self.result_mode not in ("structured", "text"):
            raise ValueError("MCP registration requires a reviewed read tool and result mode")
        if (not bounded_reference(self.alias) or not bounded_reference(self.remote_name)
                or not bounded_reference(self.binding_version)):
            raise ValueError("invalid MCP tool identity")
        if not callable(self.projector) or type(self.result_contract) is not ToolResultContractV1:
            raise ValueError("MCP registration requires a projector and result contract")
        if self.event_policy is not None and type(self.event_policy) is not ToolEventPolicyV1:
            raise ValueError("invalid MCP event policy")


def _operation_key(_arguments, context):
    return "mcp-v1." + digest([context.scope.invocation_id, context.call_index])


@dataclass(frozen=True)
class _McpHandler:
    client: object
    spec: McpToolSpecV1
    binding: str
    schemas: bytes

    def version(self, parameters):
        return "mcp-v1." + digest({
            "catalogBinding": self.binding, "remoteName": self.spec.remote_name,
            "schemas": json.loads(self.schemas), "arguments": parameters, "mode": self.spec.result_mode,
            "result": self.spec.result_contract.to_dict(),
            "event": self.spec.event_policy.to_dict() if self.spec.event_policy is not None else None,
            "version": self.spec.binding_version, "limits": vars(self.client.limits),
        })

    def validate_registration(self, tool):
        if (tool.name != self.spec.alias or tool.description != self.spec.description
                or not tool.with_context or tool.terminal or tool.parallel_read
                or tool.operation_key is not _operation_key or tool.authorize != self.authorize
                or tool.result_contract != self.spec.result_contract or tool.event_policy != self.spec.event_policy
                or tool.max_argument_bytes != self.client.limits.max_argument_bytes
                or tool.max_result_bytes != self.client.limits.max_result_bytes
                or tool.version != self.version(tool.parameters)):
            raise ValueError("MCP registration differs from its host binding")

    def authorize(self, arguments, context):
        try:
            scope = self.client._scope_key(context)
            return (self.client._catalog_binding(scope) == self.binding and
                    self.client._authorize(self.spec.alias, deepcopy(arguments), context) is True)
        except Exception:
            return False

    def __call__(self, arguments, context):
        from .mcp_transport import dispatch
        return dispatch(self, arguments, context)


def is_mcp_tool(tool: CustomTool) -> bool:
    return type(tool) is CustomTool and type(tool.handler) is _McpHandler


def admit_mcp_tools(tools, selected, *, surface, policy=None, scope=None, journal=None,
                    contracts=None, initial_tool=None, execution=False):
    names = {name for name in selected if is_mcp_tool(tools.get(name))}
    if not names:
        return
    if surface != "api" or initial_tool in names:
        raise ValueError("MCP tool unavailable on this execution surface")
    if (type(contracts) is not ToolContractBindingsV1 or policy is None
            or policy.max_provider_requests is None or policy.max_tool_calls is None):
        raise ValueError("MCP requires tool contracts and finite invocation budgets")
    for name in names:
        tool = tools[name]
        tool.handler.validate_registration(tool)
    if execution:
        if scope != contracts.scope or scope.operation_namespace is None or journal is None:
            raise ValueError("MCP requires an owned scope and tool journal")
        require_mcp_sdk()


class McpClientV1:
    def __init__(self, server, *, allow_destination, credentials, scope_key, authorize, limits=None):
        if (type(server) is not McpServerV1 or
                not all(callable(callback) for callback in (allow_destination, credentials, scope_key, authorize))):
            raise ValueError("invalid MCP client configuration")
        if limits is not None and type(limits) is not McpLimitsV1:
            raise ValueError("invalid MCP limits")
        self._server, self._limits = server, limits if limits is not None else McpLimitsV1()
        self._allow_destination, self._credentials = allow_destination, credentials
        self._scope_key, self._authorize = scope_key, authorize
        self._lock = Lock()
        self._lifecycle = None

    @property
    def server(self):
        return self._server

    @property
    def limits(self):
        return self._limits

    def _catalog_binding(self, scope):
        if not bounded_reference(scope):
            raise ValueError("invalid MCP access scope")
        return digest([self._server_binding(), scope])

    def _server_binding(self):
        return digest({"server": self.server.server_id, "revision": self.server.revision,
                       "endpoint": self.server.endpoint, "protocol": self.server.protocol_version,
                       "auth": self.server.auth_mode})

    def restore_catalog(self, document, *, access_scope, expected_sha256):
        catalog = McpCatalogV1(document, limits=self.limits, binding=self._catalog_binding(access_scope),
                               server=self._server_binding())
        if catalog.sha256 != expected_sha256:
            raise ValueError("MCP catalogue digest differs")
        return catalog

    def bind_tools(self, catalog, specs, *, existing=None):
        if type(catalog) is not McpCatalogV1:
            raise ValueError("invalid MCP catalogue")
        document = catalog.to_dict()
        catalog = McpCatalogV1(document, limits=self.limits, binding=document["binding"],
                               server=self._server_binding())
        tools = {entry["name"]: entry for entry in document["tools"]}
        registry = validate_custom_tools(existing)
        for spec in specs:
            if type(spec) is not McpToolSpecV1 or spec.remote_name not in tools:
                raise ValueError("unknown MCP tool")
            if spec.alias in registry:
                raise ValueError("MCP alias collision")
            record = tools[spec.remote_name]
            remote_input = admitted_schema(record["inputSchema"], self.limits.max_schema_bytes, input_schema=True)
            remote_output = record["outputSchema"]
            if remote_output is not None:
                remote_output = admitted_schema(remote_output, self.limits.max_schema_bytes)
            local = admitted_schema(spec.argument_schema if spec.argument_schema is not None else remote_input,
                                    self.limits.max_schema_bytes, input_schema=True, closed=True)
            schemas = {"inputSchema": remote_input, "outputSchema": remote_output}
            handler = _McpHandler(self, spec, document["binding"], canonical(schemas))
            registry[spec.alias] = CustomTool(
                spec.alias, spec.description, local, handler, handler.version(local),
                with_context=True, authorize=handler.authorize, operation_key=_operation_key,
                max_argument_bytes=self.limits.max_argument_bytes,
                max_result_bytes=self.limits.max_result_bytes,
                result_contract=spec.result_contract, event_policy=spec.event_policy,
            )
        return registry

    def __enter__(self):
        require_mcp_sdk()
        from .mcp_transport import open_client
        open_client(self)
        return self

    def __exit__(self, *_exc):
        from .mcp_transport import close_client
        close_client(self)

    def discover(self, *, access_scope):
        from .mcp_transport import discover
        return discover(self, access_scope)
