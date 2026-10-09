"""Internal transport result shared by provider adapters."""
from dataclasses import dataclass


@dataclass
class ProviderTurn:
    text: str
    finish_reason: str | None
    token_usage: int
    token_usage_details: dict[str, int]
    raw_response_metadata: dict[str, object]
    reasoning_content_observed: bool
    tool_calls: list[dict[str, object]]
    streamed: bool
    http_status: int | None
    raw_response_headers: dict[str, str]
    previewed: bool = False

    continuation: list[dict[str, object]] | None = None
