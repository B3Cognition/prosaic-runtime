from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from pathlib import Path

from harness.ai_cli_backend import CliRunRequest, CliRunResult
from harness.config import HarnessConfig


class OpenAICompatibleBackend:
    name = "openai-compatible"

    def __init__(self, config: HarnessConfig) -> None:
        self._config = config

    def run_prompt(self, request: CliRunRequest) -> CliRunResult:
        llm = self._config.llm
        assert llm.base_url is not None
        assert llm.model is not None
        streaming = _feature_enabled(llm.features, "streaming", default=True)
        payload: dict[str, object] = {
            "model": llm.model,
            "messages": [{"role": "user", "content": request.prompt}],
            "temperature": llm.temperature,
        }
        if llm.max_tokens is not None:
            payload["max_tokens"] = llm.max_tokens
        if streaming:
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        token, token_error = _api_key(llm.api_key_env, llm.api_key_file, request.env)
        if token_error:
            return CliRunResult(
                exit_code=1,
                stdout="",
                stderr=token_error,
                metadata={
                    "provider": self.name,
                    "provider_error_code": "api_key_file_error",
                },
            )
        if token:
            headers["Authorization"] = f"Bearer {token}"
        http_request = urllib.request.Request(
            f"{llm.base_url.rstrip('/')}/chat/completions",
            data=data,
            headers=headers,
            method="POST",
        )
        deadline = time.monotonic() + max(0.001, request.timeout_s)
        try:
            with urllib.request.urlopen(
                http_request, timeout=request.timeout_s
            ) as response:
                if streaming and _is_sse_response(response):
                    return self._read_sse_response(response, deadline)
                body = response.read().decode("utf-8", errors="replace")
        except TimeoutError as exc:
            return _timeout_result(self.name, str(exc))
        except socket.timeout as exc:
            return _timeout_result(self.name, str(exc))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return CliRunResult(
                exit_code=int(exc.code),
                stdout="",
                stderr=body or str(exc),
                metadata={
                    "provider": self.name,
                    "http_status": int(exc.code),
                    "provider_error_code": "http_error",
                },
            )
        except urllib.error.URLError as exc:
            return CliRunResult(
                exit_code=1,
                stdout="",
                stderr=str(exc.reason),
                metadata={"provider": self.name, "provider_error_code": "url_error"},
            )
        except OSError as exc:
            return CliRunResult(
                exit_code=1,
                stdout="",
                stderr=str(exc),
                metadata={"provider": self.name, "provider_error_code": "os_error"},
            )
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            if streaming and _looks_like_sse_body(body):
                return self._read_sse_body(body)
            return CliRunResult(
                exit_code=1,
                stdout="",
                stderr=f"Malformed OpenAI-compatible response: {body}",
                metadata={
                    "provider": self.name,
                    "streamed": False,
                    "provider_error_code": "malformed_response",
                },
            )
        if _has_tool_calls(parsed):
            return _unsupported_tool_calls_result(self.name)
        text = _assistant_text(parsed)
        if text:
            print(text, flush=True)
        metadata = {
            "provider": self.name,
            "streamed": False,
            "finish_reason": _finish_reason(parsed),
            "reasoning_content_observed": _has_reasoning_content(parsed),
        }
        return CliRunResult(
            exit_code=0,
            stdout=text,
            stderr="",
            token_usage=_token_usage(parsed),
            metadata=metadata,
        )

    def run_agent(self, request: CliRunRequest) -> CliRunResult:
        return self.run_prompt(request)

    def _read_sse_response(self, response: object, deadline: float) -> CliRunResult:
        text_parts: list[str] = []
        token_usage = 0
        finish_reason = ""
        reasoning_content_observed = False
        event_data: list[str] = []

        def handle_event(raw_data: str) -> CliRunResult | None:
            nonlocal token_usage, finish_reason, reasoning_content_observed
            if raw_data == "[DONE]":
                return None
            try:
                event = json.loads(raw_data)
            except json.JSONDecodeError:
                return CliRunResult(
                    exit_code=1,
                    stdout="",
                    stderr=f"Malformed OpenAI-compatible SSE event: {raw_data}",
                    metadata={
                        "provider": self.name,
                        "streamed": True,
                        "provider_error_code": "malformed_sse",
                    },
                )
            if _event_has_tool_calls(event):
                return _unsupported_tool_calls_result(self.name)
            event_usage = _token_usage(event)
            if event_usage:
                token_usage = event_usage
            event_finish_reason = _event_finish_reason(event)
            if event_finish_reason:
                finish_reason = event_finish_reason
            reasoning = _event_reasoning_content(event)
            if reasoning:
                reasoning_content_observed = True
            content = _event_content(event)
            if content:
                text_parts.append(content)
            return None

        while True:
            if time.monotonic() > deadline:
                return _timeout_result(self.name, "OpenAI-compatible stream exceeded deadline")
            try:
                raw_line = response.readline()  # type: ignore[attr-defined]
            except (TimeoutError, socket.timeout) as exc:
                return _timeout_result(self.name, str(exc))
            if not raw_line:
                break
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                if event_data:
                    maybe_result = handle_event("\n".join(event_data))
                    event_data = []
                    if maybe_result is not None:
                        return maybe_result
                continue
            if line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue
            data = line.removeprefix("data:").strip()
            if data == "[DONE]":
                break
            event_data.append(data)
            maybe_result = handle_event("\n".join(event_data))
            event_data = []
            if maybe_result is not None:
                return maybe_result

        if event_data:
            maybe_result = handle_event("\n".join(event_data))
            if maybe_result is not None:
                return maybe_result

        text = "".join(text_parts)
        if text:
            print(text, flush=True)
        return CliRunResult(
            exit_code=0,
            stdout=text,
            stderr="",
            token_usage=token_usage,
            metadata={
                "provider": self.name,
                "streamed": True,
                "finish_reason": finish_reason or None,
                "reasoning_content_observed": reasoning_content_observed,
            },
        )

    def _read_sse_body(self, body: str) -> CliRunResult:
        class _BodyResponse:
            def __init__(self, text: str) -> None:
                self._lines = iter(text.splitlines(keepends=True))

            def readline(self) -> bytes:
                try:
                    return next(self._lines).encode("utf-8")
                except StopIteration:
                    return b""

        return self._read_sse_response(_BodyResponse(body), time.monotonic() + 60.0)


def _api_key(
    api_key_env: str | None,
    api_key_file: str | None = None,
    request_env: object = None,
) -> tuple[str, str]:
    if api_key_env:
        token = ""
        if isinstance(request_env, Mapping):
            token = str(request_env.get(api_key_env, "")).strip()
        if not token:
            token = os.environ.get(api_key_env, "").strip()
        if token:
            return token, ""
    if not api_key_file:
        return "", ""
    path = Path(api_key_file).expanduser()
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return "", f"OpenAI-compatible API key file is not readable: {path}: {exc}"
    if not token:
        return "", f"OpenAI-compatible API key file is empty: {path}"
    return token, ""


def _feature_enabled(
    features: dict[str, object], name: str, *, default: bool
) -> bool:
    value = features.get(name, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _header(response: object, name: str) -> str:
    getter = getattr(response, "getheader", None)
    if callable(getter):
        value = getter(name)
        return value if isinstance(value, str) else ""
    headers = getattr(response, "headers", None)
    if hasattr(headers, "get"):
        value = headers.get(name)  # type: ignore[call-arg]
        return value if isinstance(value, str) else ""
    return ""


def _is_sse_response(response: object) -> bool:
    return "text/event-stream" in _header(response, "Content-Type").lower()


def _looks_like_sse_body(body: str) -> bool:
    for line in body.splitlines():
        stripped = line.lstrip()
        if not stripped:
            continue
        return stripped.startswith("data:") or stripped.startswith(":")
    return False


def _assistant_text(parsed: object) -> str:
    if not isinstance(parsed, dict):
        return ""
    choices = parsed.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if isinstance(message, dict):
        content = _content_text(message.get("content"))
        if content:
            return content
    text = first.get("text")
    return text if isinstance(text, str) else ""


def _finish_reason(parsed: object) -> str | None:
    if not isinstance(parsed, dict):
        return None
    choices = parsed.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    finish_reason = first.get("finish_reason")
    return finish_reason if isinstance(finish_reason, str) else None


def _has_reasoning_content(parsed: object) -> bool:
    if not isinstance(parsed, dict):
        return False
    choices = parsed.get("choices")
    if not isinstance(choices, list):
        return False
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if isinstance(message, dict) and isinstance(message.get("reasoning_content"), str):
            return True
        delta = choice.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("reasoning_content"), str):
            return True
    return False


def _has_tool_calls(parsed: object) -> bool:
    if not isinstance(parsed, dict):
        return False
    choices = parsed.get("choices")
    if not isinstance(choices, list):
        return False
    return any(_choice_has_tool_calls(choice) for choice in choices)


def _choice_has_tool_calls(choice: object) -> bool:
    if not isinstance(choice, dict):
        return False
    message = choice.get("message")
    if isinstance(message, dict) and message.get("tool_calls"):
        return True
    delta = choice.get("delta")
    return isinstance(delta, dict) and bool(delta.get("tool_calls"))


def _event_has_tool_calls(event: object) -> bool:
    return _has_tool_calls(event)


def _event_content(event: object) -> str:
    delta = _first_delta(event)
    if not delta:
        return ""
    return _content_text(delta.get("content"))


def _event_reasoning_content(event: object) -> str:
    delta = _first_delta(event)
    if not delta:
        return ""
    content = delta.get("reasoning_content")
    return content if isinstance(content, str) else ""


def _event_finish_reason(event: object) -> str:
    if not isinstance(event, dict):
        return ""
    choices = event.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    finish_reason = first.get("finish_reason")
    return finish_reason if isinstance(finish_reason, str) else ""


def _first_delta(event: object) -> dict[str, object] | None:
    if not isinstance(event, dict):
        return None
    choices = event.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    delta = first.get("delta")
    return delta if isinstance(delta, dict) else None


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _token_usage(parsed: object) -> int:
    if not isinstance(parsed, dict):
        return 0
    usage = parsed.get("usage")
    if not isinstance(usage, dict):
        return 0
    total = usage.get("total_tokens")
    if isinstance(total, int):
        return total
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    total_tokens = 0
    if isinstance(prompt, int):
        total_tokens += prompt
    if isinstance(completion, int):
        total_tokens += completion
    return total_tokens


def _timeout_result(provider: str, message: str) -> CliRunResult:
    return CliRunResult(
        exit_code=-1,
        stdout="",
        stderr=message,
        timed_out=True,
        metadata={"provider": provider, "provider_error_code": "timeout"},
    )


def _unsupported_tool_calls_result(provider: str) -> CliRunResult:
    return CliRunResult(
        exit_code=1,
        stdout="",
        stderr="OpenAI-compatible artifact provider tool_calls are not supported",
        metadata={
            "provider": provider,
            "provider_error_code": "unsupported_tool_calls",
        },
    )
