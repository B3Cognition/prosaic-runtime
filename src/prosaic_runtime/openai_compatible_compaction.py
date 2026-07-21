"""Request-time tool-result compaction for OpenAI-compatible conversations."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Mapping


@dataclass(frozen=True)
class ToolResultCompactionStats:
    tool_results: int = 0
    compacted: int = 0
    original_chars: int = 0
    compacted_chars: int = 0

    @property
    def saved_chars(self) -> int:
        return max(0, self.original_chars - self.compacted_chars)


def compact_tool_result_messages(
    messages: list[dict[str, object]],
    features: Mapping[str, object],
) -> tuple[list[dict[str, object]], ToolResultCompactionStats]:
    if not _feature_bool(features, "tool_result_compaction", default=True):
        return list(messages), ToolResultCompactionStats()
    tool_indices = [
        index
        for index, message in enumerate(messages)
        if message.get("role") == "tool"
    ]
    compact_after = _feature_int(
        features,
        "compact_after_tool_results",
        default=8,
        minimum=0,
        maximum=10_000,
    )
    if len(tool_indices) <= compact_after:
        return list(messages), ToolResultCompactionStats(tool_results=len(tool_indices))
    keep_recent = _feature_int(
        features,
        "keep_recent_tool_results",
        default=4,
        minimum=0,
        maximum=10_000,
    )
    threshold = _feature_int(
        features,
        "compact_tool_result_after_chars",
        default=96_000,
        minimum=1,
        maximum=10_000_000,
    )
    summary_limit = _feature_int(
        features,
        "compacted_result_chars",
        default=2_000,
        minimum=20,
        maximum=100_000,
    )
    keep_indices = set(tool_indices[-keep_recent:]) if keep_recent else set()
    call_names = _tool_call_names(messages)
    compacted_messages = [dict(message) for message in messages]
    compacted = 0
    original_chars = 0
    compacted_chars = 0
    for index in tool_indices:
        if index in keep_indices:
            continue
        message = compacted_messages[index]
        content = message.get("content")
        if not isinstance(content, str) or len(content) <= threshold:
            continue
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        status = payload.get("status")
        if status == "error":
            continue
        tool_call_id = message.get("tool_call_id")
        tool_name = call_names.get(tool_call_id, "unknown") if isinstance(tool_call_id, str) else "unknown"
        replacement = {
            "status": "compacted",
            "original_status": status if isinstance(status, str) else "",
            "tool_name": tool_name,
            "tool_call_id": tool_call_id if isinstance(tool_call_id, str) else "",
            "original_chars": len(content),
            "summary": _compact_summary(payload, tool_name, limit=summary_limit),
        }
        replacement_content = json.dumps(replacement, sort_keys=True)
        message["content"] = replacement_content
        compacted += 1
        original_chars += len(content)
        compacted_chars += len(replacement_content)
    return compacted_messages, ToolResultCompactionStats(
        tool_results=len(tool_indices),
        compacted=compacted,
        original_chars=original_chars,
        compacted_chars=compacted_chars,
    )


def _tool_call_names(messages: list[dict[str, object]]) -> dict[str, str]:
    names: dict[str, str] = {}
    for message in messages:
        raw_calls = message.get("tool_calls")
        if not isinstance(raw_calls, list):
            continue
        for raw_call in raw_calls:
            if not isinstance(raw_call, dict):
                continue
            call_id = raw_call.get("id")
            function = raw_call.get("function")
            if not isinstance(call_id, str) or not isinstance(function, dict):
                continue
            name = function.get("name")
            if isinstance(name, str) and name:
                names[call_id] = name
    return names


def _compact_summary(payload: dict[str, object], tool_name: str, *, limit: int) -> str:
    parts = [tool_name]
    status = payload.get("status")
    if isinstance(status, str) and status:
        parts.append(f"status={status}")
    for key in ("path", "pattern", "file_pattern", "query", "url", "run_dir", "source_id", "domain_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            parts.append(f"{key}={value}")
    for key in ("line_count", "bytes", "replacements"):
        value = payload.get(key)
        if isinstance(value, int):
            parts.append(f"{key}={value}")
    truncated = payload.get("truncated")
    if isinstance(truncated, bool):
        parts.append(f"truncated={str(truncated).lower()}")
    content = payload.get("content")
    if isinstance(content, str):
        parts.append(f"content_chars={len(content)}")
    files = payload.get("files")
    if isinstance(files, dict):
        parts.append(_preview_keys("files", list(files.keys())))
    elif isinstance(files, list):
        parts.append(_preview_items("files", files))
    for key in ("matches", "entries", "source_files", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            parts.append(_preview_items(key, value))
    summary = "; ".join(part for part in parts if part)
    if len(summary) <= limit:
        return summary
    return summary[: limit - 3] + "..."


def _preview_keys(label: str, values: list[object], *, limit: int = 6) -> str:
    strings = [value for value in values if isinstance(value, str) and value]
    preview = ", ".join(strings[:limit])
    if len(strings) > limit:
        preview += f", +{len(strings) - limit} more"
    return f"{label}={len(values)} [{preview}]" if preview else f"{label}={len(values)}"


def _preview_items(label: str, values: list[object], *, limit: int = 6) -> str:
    previews: list[str] = []
    for value in values:
        if isinstance(value, Mapping):
            for key in ("path", "url", "title"):
                item = value.get(key)
                if isinstance(item, str) and item:
                    previews.append(item)
                    break
        elif isinstance(value, str) and value:
            previews.append(value)
        if len(previews) >= limit:
            break
    preview = ", ".join(previews)
    if len(values) > len(previews):
        preview += f", +{len(values) - len(previews)} more"
    return f"{label}={len(values)} [{preview}]" if preview else f"{label}={len(values)}"


def _feature_bool(
    features: Mapping[str, object],
    name: str,
    *,
    default: bool,
) -> bool:
    value = features.get(name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _feature_int(
    features: Mapping[str, object],
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = features.get(name, default)
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
    else:
        return default
    return max(minimum, min(maximum, parsed))
