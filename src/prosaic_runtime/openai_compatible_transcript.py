"""Compact transcript artifacts for the OpenAI-compatible provider."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Mapping


class ProviderTranscript:
    def __init__(self, path: Path | None) -> None:
        self.path = path

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def write(self, event: str, **payload: object) -> None:
        if self.path is None:
            return
        record = {
            "schema_version": 1,
            "provider": "openai-compatible",
            "event": event,
            "event_time": datetime.now(timezone.utc).isoformat(),
        }
        record.update(payload)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")


def open_provider_transcript(
    cwd: Path,
    features: Mapping[str, object],
    request_metadata: Mapping[str, object],
) -> ProviderTranscript:
    if _disabled(features.get("transcript")) or _disabled(
        features.get("provider_transcript")
    ):
        return ProviderTranscript(None)
    base = _configured_dir(features, request_metadata)
    if base is None:
        base = _detect_run_dir(cwd)
    if base is None:
        return ProviderTranscript(None)
    label = _safe_label(request_metadata.get("provider_transcript_label"))
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return ProviderTranscript(
        base / "provider-transcripts" / f"openai-compatible-{label}-{timestamp}.jsonl"
    )


def _disabled(value: object) -> bool:
    if isinstance(value, bool):
        return not value
    if isinstance(value, str):
        return value.strip().lower() in {"0", "false", "no", "off", "disabled"}
    return False


def _configured_dir(
    features: Mapping[str, object],
    request_metadata: Mapping[str, object],
) -> Path | None:
    for value in (
        request_metadata.get("provider_transcript_dir"),
        request_metadata.get("run_dir"),
        features.get("provider_transcript_dir"),
    ):
        if isinstance(value, str) and value.strip():
            return Path(value).expanduser().resolve(strict=False)
    return None


def _detect_run_dir(cwd: Path) -> Path | None:
    resolved = cwd.resolve(strict=False)
    if (
        (resolved / "state.json").exists()
        or (resolved / "re-execution-plan.json").exists()
        or (resolved / "re-source-index.json").exists()
    ):
        return resolved
    if "runs" in resolved.parts:
        return resolved
    return None


def _safe_label(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return "run"
    label = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value)
    return label.strip("-") or "run"
