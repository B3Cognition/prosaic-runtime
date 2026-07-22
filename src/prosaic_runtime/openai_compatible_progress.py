"""Progress and compact-summary helpers for the OpenAI-compatible provider."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Mapping

_LLM_PREFIX = "  llm | "


def _progress(message: str) -> None:
    print(f"[openai-compatible] {message}", file=sys.stderr, flush=True)


def _progress_llm_preview(text: str, *, limit: int = 600) -> bool:
    preview = text.replace("\r\n", "\n").replace("\r", "\n")
    if not preview.strip():
        return False
    truncated = False
    if len(preview) > limit:
        preview = preview[:limit]
        truncated = True
    lines = preview.splitlines() or [preview]
    for line in lines[:8]:
        _print_llm_line(line)
    if truncated or len(lines) > 8:
        _print_llm_line("...")
    return True


class OpenAIStreamPreview:
    def __init__(
        self,
        *,
        enabled: bool = True,
        flush_chars: int = 500,
        flush_interval_s: float = 2.0,
        max_chars: int = 1200,
        max_lines: int = 12,
    ) -> None:
        self._enabled = enabled
        self._flush_chars = flush_chars
        self._flush_interval_s = flush_interval_s
        self._buffer = ""
        self._last_flush = time.monotonic()
        self._max_chars = max(1, max_chars)
        self._max_lines = max(1, max_lines)
        self._emitted_chars = 0
        self._emitted_lines = 0
        self._emitted = False
        self._truncated = False

    @property
    def emitted(self) -> bool:
        return self._emitted

    @property
    def truncated(self) -> bool:
        return self._truncated

    def append(self, text: str) -> None:
        if not self._enabled or self._truncated:
            return
        self._buffer += text.replace("\r\n", "\n").replace("\r", "\n")
        self._flush_complete_lines()
        if len(self._buffer) >= self._flush_chars:
            self.flush()
            return
        if self._buffer.strip() and time.monotonic() - self._last_flush >= self._flush_interval_s:
            self.flush()

    def flush(self) -> None:
        if not self._enabled or self._truncated or not self._buffer:
            return
        text = self._buffer
        self._buffer = ""
        self._last_flush = time.monotonic()
        for line in text.splitlines() or [text]:
            if line.strip():
                self._emit_line(line)
            if self._truncated:
                break

    def _flush_complete_lines(self) -> None:
        if "\n" not in self._buffer:
            return
        lines = self._buffer.split("\n")
        self._buffer = lines.pop()
        self._last_flush = time.monotonic()
        for line in lines:
            if line.strip():
                self._emit_line(line)
            if self._truncated:
                self._buffer = ""
                break

    def _emit_line(self, line: str) -> None:
        if self._truncated:
            return
        if self._emitted_lines >= self._max_lines:
            self._mark_truncated()
            return
        rendered = _truncate_llm_preview_line(line)
        remaining = self._max_chars - self._emitted_chars
        if len(rendered) > remaining:
            if not self._emitted and remaining > 0:
                _print_llm_line(rendered[:remaining])
                self._emitted = True
                self._emitted_lines += 1
                self._emitted_chars += remaining
            self._mark_truncated()
            return
        _print_llm_line(rendered)
        self._emitted = True
        self._emitted_lines += 1
        self._emitted_chars += len(rendered)

    def _mark_truncated(self) -> None:
        if self._truncated:
            return
        self._truncated = True
        self._buffer = ""
        _print_llm_line("... preview truncated ...")


def _progress_turn_summary(
    turn_number: int,
    *,
    model_elapsed: float,
    tool_elapsed: float,
    model_text_chars: int,
    turn_tool_calls: int,
    tool_rounds: int,
    max_tool_rounds: int,
    tool_call_count: int,
) -> None:
    _progress(
        f"turn {turn_number} summary: "
        f"model_time={model_elapsed:.1f}s "
        f"tool_time={tool_elapsed:.1f}s "
        f"model_text={model_text_chars} chars "
        f"tool_calls={turn_tool_calls} "
        f"tool_budget={tool_rounds}/{max_tool_rounds} "
        f"calls_total={tool_call_count}"
    )


def _elapsed_s(started_at: float) -> str:
    return f"{time.monotonic() - started_at:.1f}"


def _single_line_preview(text: str, *, limit: int = 180) -> str:
    if not text.strip():
        return ""
    return _truncate_progress_value(text, limit=limit)


def _event_tool_call_delta_summaries(event: object) -> list[str]:
    choice = _first_choice(event)
    if not choice:
        return []
    summaries: list[str] = []
    for container_name in ("delta", "message"):
        container = choice.get(container_name)
        if not isinstance(container, dict):
            continue
        raw_calls = container.get("tool_calls")
        if not isinstance(raw_calls, list):
            continue
        for offset, raw_call in enumerate(raw_calls):
            if not isinstance(raw_call, dict):
                continue
            index = raw_call.get("index")
            if not isinstance(index, int):
                index = offset
            raw_function = raw_call.get("function")
            if not isinstance(raw_function, dict):
                summaries.append(f"index={index}")
                continue
            parts = [f"index={index}"]
            name = raw_function.get("name")
            if isinstance(name, str) and name:
                parts.append(f"name={name}")
            arguments = raw_function.get("arguments")
            if isinstance(arguments, str):
                parts.append(f"args_chars={len(arguments)}")
            summaries.append(" ".join(parts))
    return summaries


def _tool_call_name(tool_call: dict[str, object]) -> str:
    function = tool_call.get("function")
    if not isinstance(function, dict):
        return "unknown"
    name = function.get("name")
    return name if isinstance(name, str) and name else "unknown"


def _tool_call_summary(tool_call: dict[str, object]) -> str:
    function = tool_call.get("function")
    if not isinstance(function, dict):
        return ""
    raw_arguments = function.get("arguments")
    if not isinstance(raw_arguments, str):
        return ""
    try:
        arguments = json.loads(raw_arguments or "{}")
    except json.JSONDecodeError:
        return "malformed-arguments"
    if not isinstance(arguments, dict):
        return "non-object-arguments"
    parts: list[str] = []
    for key in ("path", "filePath", "file_path", "url", "query", "pattern"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            parts.append(_truncate_progress_value(value))
            break
    run_dir = arguments.get("run_dir")
    if isinstance(run_dir, str) and run_dir:
        parts.append("run_dir=" + _truncate_progress_value(run_dir, limit=80))
    source_id = arguments.get("source_id")
    if isinstance(source_id, str) and source_id:
        parts.append(f"source_id={source_id}")
    domain_id = arguments.get("domain_id")
    if isinstance(domain_id, str) and domain_id:
        parts.append(f"domain_id={domain_id}")
    paths = arguments.get("paths")
    if isinstance(paths, list):
        string_paths = [item for item in paths if isinstance(item, str) and item]
        if string_paths:
            preview = ", ".join(
                _truncate_progress_value(item, limit=50)
                for item in string_paths[:3]
            )
            if len(string_paths) > 3:
                preview += f", +{len(string_paths) - 3} more"
            parts.append(f"paths={len(string_paths)} [{preview}]")
    for key in (
        "file_pattern",
        "max_entries",
        "max_matches",
        "max_files",
        "limit",
        "limit_per_file",
        "max_total_chars",
        "max_chars_per_file",
        "before",
        "after",
    ):
        value = arguments.get(key)
        if isinstance(value, (int, str)) and not isinstance(value, bool):
            parts.append(f"{key}={value}")
    return " ".join(parts) if parts else "{}"


def _tool_result_status(tool_message: dict[str, object]) -> str:
    content = tool_message.get("content")
    if not isinstance(content, str):
        return "unknown"
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return "unknown"
    if not isinstance(parsed, dict):
        return "unknown"
    status = parsed.get("status")
    if isinstance(status, str) and status:
        if status == "error":
            error = parsed.get("error")
            if isinstance(error, str) and error:
                return "error " + _truncate_progress_value(error)
        details = _tool_result_details(parsed)
        return status if not details else f"{status} {details}"
    return "unknown"


def _tool_result_details(payload: dict[str, object]) -> str:
    detail_parts: list[str] = []
    http_status = payload.get("http_status")
    if isinstance(http_status, int):
        detail_parts.append(f"http_status={http_status}")
    bytes_written = payload.get("bytes")
    if isinstance(bytes_written, int):
        detail_parts.append(f"bytes={bytes_written}")
    path = payload.get("path")
    if isinstance(path, str) and path:
        detail_parts.append("path=" + _truncate_progress_value(path, limit=80))
    line_count = payload.get("line_count")
    if isinstance(line_count, int):
        detail_parts.append(f"line_count={line_count}")
    replacements = payload.get("replacements")
    if isinstance(replacements, int):
        detail_parts.append(f"replacements={replacements}")
    content = payload.get("content")
    if isinstance(content, str):
        detail_parts.append(f"chars={len(content)}")
    for key in ("matches", "results", "entries", "source_files"):
        value = payload.get(key)
        if isinstance(value, list):
            preview = _payload_item_preview(value)
            if preview:
                detail_parts.append(f"{key}={len(value)} [{preview}]")
            else:
                detail_parts.append(f"{key}={len(value)}")
    files = payload.get("files")
    if isinstance(files, dict):
        preview = _payload_mapping_key_preview(files)
        if preview:
            detail_parts.append(f"files={len(files)} [{preview}]")
        else:
            detail_parts.append(f"files={len(files)}")
    elif isinstance(files, list):
        preview = _payload_item_preview(files)
        if preview:
            detail_parts.append(f"files={len(files)} [{preview}]")
        else:
            detail_parts.append(f"files={len(files)}")
    truncated = payload.get("truncated")
    if isinstance(truncated, bool):
        detail_parts.append(f"truncated={str(truncated).lower()}")
    return " ".join(detail_parts)


def _truncate_progress_value(value: str, limit: int = 160) -> str:
    value = " ".join(value.split())
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def _truncate_llm_preview_line(value: str, limit: int = 180) -> str:
    value = value.rstrip()
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def _print_llm_line(line: str) -> None:
    print(
        f"{_LLM_PREFIX}{_truncate_llm_preview_line(line)}",
        file=sys.stderr,
        flush=True,
    )


def _payload_item_preview(items: list[object], *, limit: int = 3) -> str:
    values: list[str] = []
    for item in items:
        if isinstance(item, Mapping):
            path = _mapping_str(item, "path") or _mapping_str(item, "url")
            if path:
                line = item.get("line")
                if isinstance(line, int):
                    path = f"{path}:{line}"
                values.append(path)
                if len(values) >= limit:
                    break
                continue
            title = _mapping_str(item, "title")
            if title:
                values.append(title)
        elif isinstance(item, str) and item:
            values.append(item)
        if len(values) >= limit:
            break
    return _preview_values(values, total=len(items), limit=limit)


def _payload_mapping_key_preview(
    items: Mapping[str, object],
    *,
    limit: int = 3,
) -> str:
    return _preview_values(list(items.keys())[:limit], total=len(items), limit=limit)


def _preview_values(values: list[str], *, total: int, limit: int) -> str:
    if not values:
        return ""
    preview = ", ".join(
        _truncate_progress_value(value, limit=50)
        for value in values
    )
    remaining = total - len(values)
    if remaining > 0:
        preview += f", +{remaining} more"
    return preview


def _mapping_str(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    return item.strip() if isinstance(item, str) else ""


def _first_choice(parsed: object) -> dict[str, object]:
    if not isinstance(parsed, dict):
        return {}
    choices = parsed.get("choices")
    if not isinstance(choices, list) or not choices:
        return {}
    first = choices[0]
    return first if isinstance(first, dict) else {}
