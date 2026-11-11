"""Pure MCP catalogue identity and the supported schema profile."""

from dataclasses import dataclass, fields
from hashlib import sha256
import ipaddress
import json
import math
import re
from urllib.parse import urlsplit, urlunsplit

from jsonschema import Draft202012Validator

from .conversation import encode_owned
from .operation_context import bounded_reference


MODERN = "2026-07-28"
LEGACY = "2025-11-25"


def canonical(value, limit=65536):
    owned = json.loads(encode_owned(value, max_bytes=limit))
    return json.dumps(owned, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def digest(value):
    return sha256(canonical(value)).hexdigest()


@dataclass(frozen=True)
class McpLimitsV1:
    max_inflight: int = 4
    discovery_timeout_s: float = 10
    call_timeout_s: float = 20
    cleanup_timeout_s: float = 1
    max_list_pages: int = 8
    max_catalog_tools: int = 128
    max_catalog_bytes: int = 65536
    max_schema_bytes: int = 16384
    max_argument_bytes: int = 16384
    max_transport_bytes: int = 131072
    max_sse_events: int = 64
    max_sse_event_bytes: int = 32768
    max_result_bytes: int = 32768

    def __post_init__(self):
        for field in fields(self):
            name = field.name
            value = getattr(self, name)
            ceiling = 16 if name == "max_inflight" else field.default
            types = (int, float) if name.endswith("_s") else (int,)
            if type(value) not in types or not 0 < value <= ceiling or not math.isfinite(value):
                raise ValueError("invalid MCP limit")


@dataclass(frozen=True)
class McpServerV1:
    server_id: str
    revision: str
    endpoint: str
    protocol_version: str
    auth_mode: str = "bearer"
    allow_loopback_http: bool = False

    def __post_init__(self):
        if not bounded_reference(self.server_id) or not bounded_reference(self.revision):
            raise ValueError("invalid MCP server identity")
        if self.protocol_version not in (MODERN, LEGACY) or self.auth_mode not in ("bearer", "none"):
            raise ValueError("unsupported MCP profile")
        if type(self.allow_loopback_http) is not bool or not bounded_reference(self.endpoint, 2048):
            raise ValueError("invalid MCP endpoint")
        if not self.endpoint.isascii() or any(c.isspace() or c in "\\?#" for c in self.endpoint):
            raise ValueError("invalid MCP endpoint")
        try:
            url = urlsplit(self.endpoint)
            if (url.hostname is None or url.username is not None
                    or re.fullmatch(r"/[A-Za-z0-9_./~-]*", url.path) is None
                    or any(segment in {".", ".."} for segment in url.path.split("/"))):
                raise ValueError
            port = url.port
            host = url.hostname
            if url.scheme != "https":
                if (url.scheme != "http" or not self.allow_loopback_http or self.auth_mode != "none"
                        or not ipaddress.ip_address(host).is_loopback):
                    raise ValueError
            if "%" in host or not host or url.netloc.endswith(":"):
                raise ValueError
            authority = f"[{host}]" if ":" in host else host
            if port is not None and port != (443 if url.scheme == "https" else 80):
                authority += f":{port}"
            object.__setattr__(self, "endpoint", urlunsplit((url.scheme, authority, url.path, "", "")))
        except (ValueError, TypeError):
            raise ValueError("invalid MCP endpoint") from None


@dataclass(frozen=True, init=False)
class McpCatalogV1:
    _encoded: bytes

    def __init__(self, document, *, limits, binding, server):
        encoded = canonical(document, limits.max_catalog_bytes)
        data = json.loads(encoded)
        if (type(data) is not dict or set(data) != {"version", "binding", "server", "tools"}
                or type(data["version"]) is not int or data["version"] != 1
                or data["binding"] != binding or data["server"] != server
                or type(data["tools"]) is not list
                or len(data["tools"]) > limits.max_catalog_tools):
            raise ValueError("invalid MCP catalogue")
        names = []
        for tool in data["tools"]:
            if (type(tool) is not dict or set(tool) != {"name", "inputSchema", "outputSchema"}
                    or not bounded_reference(tool["name"]) or type(tool["inputSchema"]) is not dict
                    or (tool["outputSchema"] is not None and type(tool["outputSchema"]) is not dict)):
                raise ValueError("invalid MCP catalogue tool")
            names.append(tool["name"])
        if names != sorted(set(names)):
            raise ValueError("invalid MCP catalogue order or duplicate tool")
        object.__setattr__(self, "_encoded", encoded)

    @property
    def sha256(self):
        return sha256(self._encoded).hexdigest()

    def to_dict(self):
        return json.loads(self._encoded)


_SCHEMA_KEYWORDS = frozenset({
    "$schema", "type", "properties", "required", "additionalProperties", "items",
    "enum", "const", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "minLength", "maxLength", "minItems", "maxItems", "title", "description",
})


def strip_schema_prose(value):
    """Strip annotations at schema positions, preserving property names and literal values."""
    if type(value) is not dict:
        return value
    result = {}
    for key, child in value.items():
        if key in {"title", "description"}:
            continue
        if key == "properties" and type(child) is dict:
            child = {name: strip_schema_prose(schema) for name, schema in child.items()}
        elif key == "items":
            child = strip_schema_prose(child)
        result[key] = child
    return result


def admitted_schema(value, maximum, *, input_schema=False, closed=False):
    raw = json.loads(canonical(value, maximum))

    def visit(schema):
        if type(schema) is not dict or set(schema) - _SCHEMA_KEYWORDS:
            raise ValueError("unsupported MCP schema keyword")
        if "$schema" in schema and schema["$schema"] not in (
                "https://json-schema.org/draft/2020-12/schema",
                "https://json-schema.org/draft/2020-12/schema#"):
            raise ValueError("unsupported MCP schema dialect")
        if "additionalProperties" in schema and type(schema["additionalProperties"]) is not bool:
            raise ValueError("unsupported MCP additional properties schema")
        props = schema.get("properties", {})
        if type(props) is not dict:
            raise ValueError("invalid MCP schema properties")
        for child in props.values():
            visit(child)
        if "items" in schema:
            visit(schema["items"])

    visit(raw)
    if input_schema and raw.get("type") != "object":
        raise ValueError("MCP input schema requires an object")
    if closed and raw.get("additionalProperties") is not False:
        raise ValueError("MCP local schema requires a closed object")
    from jsonschema.exceptions import SchemaError
    try:
        Draft202012Validator.check_schema(raw)
    except SchemaError:
        raise ValueError("invalid MCP tool schema") from None
    return strip_schema_prose(raw)
