"""Opaque host correlation, independent of billing attribution."""
from dataclasses import dataclass, fields
from collections.abc import Callable
import unicodedata


def bounded_reference(value, limit=128):
    return (type(value) is str and bool(value) and len(value) <= limit
            and not any(unicodedata.category(char) in {'Cc', 'Cf', 'Cs'} for char in value)
            and len(value.encode('utf-8')) <= limit)


@dataclass(frozen=True)
class InvocationScope:
    invocation_id: str
    run_id: str | None = None
    step_id: str | None = None
    operation_namespace: str | None = None

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if (value is None and field.name != 'invocation_id'):
                continue
            if not bounded_reference(value):
                raise ValueError(f'invalid {field.name}')


@dataclass(frozen=True)
class ToolExecutionContext:
    """Trusted invocation data; business identity is resolved only by the host."""
    scope: InvocationScope
    tool_name: str
    tool_version: str
    call_index: int
    arguments_sha256: str
    deadline: float
    cancelled: Callable
    operation_key: str | None = None
