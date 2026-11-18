"""Request-time encoding of tool results for the model (profile feature ``tool_result_format``).

Tool results stay JSON everywhere inside Runtime: the journal, guards, result contracts, compaction and
conversation records all read them as data. Only the copy of the messages sent to the provider is encoded, here,
so every path that builds a request (live turns, history replay, the admitted first request) encodes alike.

``toon`` writes a list of rows with the same plain fields as one table: field names once, then one line per row.
For list-heavy results that is far fewer input tokens than JSON. Error results stay JSON, and any result that
cannot be encoded is sent as JSON unchanged.
"""

from __future__ import annotations

import json
from typing import Callable, Mapping

try:
    from toon_format import encode as _encode
except ImportError:  # optional extra: b3-prosaic-runtime[toon]
    _encode: Callable[[object], str] | None = None  # type: ignore[no-redef]

FEATURE = "tool_result_format"
FORMATS = ("json", "toon")


def validate(features: Mapping[str, object]) -> None:
    """Reject an unknown format, or ``toon`` when its library is not installed."""
    value = features.get(FEATURE, "json")
    if value not in FORMATS:
        raise ValueError(f"{FEATURE} must be one of {', '.join(FORMATS)}")
    if value == "toon" and _encode is None:
        raise ValueError(f"{FEATURE}: toon needs the toon-format package; install b3-prosaic-runtime[toon]")


def encode_tool_results(messages: list[dict[str, object]], features: Mapping[str, object],
                        cache: dict[str, str | None] | None = None) -> list[dict[str, object]]:
    """The messages with each successful tool result in the profile's format; the caller's messages are untouched.

    ``cache`` maps a result's JSON text to its encoded text (None when it stays JSON). A backend passes one that
    lives for its run, so a result is encoded once however many requests of the run send it again.
    """
    if features.get(FEATURE, "json") != "toon" or _encode is None:
        return messages
    cache = {} if cache is None else cache
    return [_encoded(message, cache) if message.get("role") == "tool" else message for message in messages]


def _encoded(message: dict[str, object], cache: dict[str, str | None]) -> dict[str, object]:
    content = message.get("content")
    if not isinstance(content, str):
        return message
    if content not in cache:
        cache[content] = _toon(content)
    encoded = cache[content]
    return message if encoded is None else {**message, "content": encoded}


def _toon(content: str) -> str | None:
    """The TOON text of a successful JSON result, or None when it is sent as it is."""
    try:
        payload = json.loads(content)
        if not isinstance(payload, dict) or payload.get("status") == "error":
            return None
        return _encode(payload)
    except Exception:  # never fail a request over its format: send the JSON as it was
        return None
