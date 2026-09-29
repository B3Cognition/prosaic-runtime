"""Transport types. Agent entry points accept Prosaic artifacts, not raw prompts."""
from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True)
class Invocation:
    cwd: str
    prompt: str
    env: Mapping[str, str]
    timeout_s: float
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass
class Result:
    exit_code: int
    stdout: str
    stderr: str
    token_usage: int | None = None
    cost_usd: float = 0.0
    timed_out: bool = False
    metadata: dict[str, object] = field(default_factory=dict)
