"""Invocation authority supplied by the host, never elevated by prose."""
from dataclasses import dataclass

READ_TOOLS = frozenset({"read_file", "read_many_files", "sha256_file", "list_files", "list_tree_with_sizes", "grep_files", "grep_context"})
WRITE_TOOLS = frozenset({"write_file", "edit_file"})
BUILTIN_TOOLS = READ_TOOLS | WRITE_TOOLS


@dataclass(frozen=True)
class RunPolicy:
    allowed_tools: frozenset[str] = frozenset()
    read_roots: tuple[str, ...] = ()
    write_paths: tuple[str, ...] = ()
    forbidden_roots: tuple[str, ...] = ()
    timeout_s: float = 120
    max_tool_rounds: int = 8
    max_input_bytes: int = 262144
    initial_tool: str | None = None

    def __post_init__(self):
        import math
        if not math.isfinite(self.timeout_s) or self.timeout_s <= 0:
            raise ValueError("timeout_s must be finite and positive")
        if type(self.max_tool_rounds) is not int or not 1 <= self.max_tool_rounds <= 64:
            raise ValueError("max_tool_rounds must be between 1 and 64")
        if type(self.max_input_bytes) is not int or self.max_input_bytes <= 0:
            raise ValueError("max_input_bytes must be positive")
        if self.initial_tool is not None and self.initial_tool not in self.allowed_tools:
            raise ValueError('initial_tool must be explicitly allowed')


def requested_tools(value) -> frozenset[str]:
    if value is None or value == "" or value == "none":
        return frozenset()
    if value == "read":
        return READ_TOOLS
    if value == "write":
        return BUILTIN_TOOLS
    if value == "full":
        raise ValueError("tools: full requires capabilities this runtime does not provide")
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return frozenset(value)
    raise ValueError("unsupported Prosaic tools declaration")
