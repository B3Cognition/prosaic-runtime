from __future__ import annotations

import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from collections.abc import Mapping
from dataclasses import dataclass
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
        prompt_metadata = _prompt_metadata(request)
        streaming = _feature_enabled(llm.features, "streaming", default=True)
        if _feature_enabled(llm.features, "tool_calls", default=False):
            return self._run_prompt_with_tools(request, prompt_metadata, streaming)
        payload: dict[str, object] = {
            "model": _metadata_str(prompt_metadata, "model") or llm.model,
            "messages": [{"role": "user", "content": request.prompt}],
            "temperature": _metadata_float(prompt_metadata, "temperature", llm.temperature),
        }
        max_tokens = _metadata_int(prompt_metadata, "max_tokens", llm.max_tokens)
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if _feature_enabled(llm.features, "json_mode", default=False):
            payload["response_format"] = {"type": "json_object"}
        reasoning_effort = (
            _metadata_reasoning_effort(prompt_metadata, "reasoning_effort")
            or _metadata_reasoning_effort(prompt_metadata, "effort")
            or _reasoning_effort(llm.features)
        )
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort
        if streaming:
            payload["stream"] = True
            if _feature_enabled(llm.features, "stream_options", default=True):
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
                http_status = _http_status(response)
                raw_response_headers = _raw_response_headers(response)
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
                    "raw_response_headers": _raw_response_headers(exc),
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
        metadata = {
            "provider": self.name,
            "streamed": False,
            "http_status": http_status,
            "raw_response_headers": raw_response_headers,
            "finish_reason": _finish_reason(parsed),
            "token_usage_details": _token_usage_details(parsed),
            "raw_response_metadata": _raw_response_metadata(parsed),
            "reasoning_content_policy": _reasoning_content_policy(llm.features),
            "reasoning_content_observed": _has_reasoning_content(parsed),
        }
        incomplete = _incomplete_finish_reason(metadata["finish_reason"])
        if incomplete:
            return _incomplete_generation_result(
                self.name,
                text,
                _token_usage(parsed),
                metadata,
                str(incomplete),
            )
        if text:
            print(text, flush=True)
        return CliRunResult(
            exit_code=0,
            stdout=text,
            stderr="",
            token_usage=_token_usage(parsed),
            metadata=metadata,
        )

    def run_agent(self, request: CliRunRequest) -> CliRunResult:
        return self.run_prompt(request)

    def _run_prompt_with_tools(
        self,
        request: CliRunRequest,
        prompt_metadata: Mapping[str, object],
        streaming: bool,
    ) -> CliRunResult:
        llm = self._config.llm
        assert llm.base_url is not None
        assert llm.model is not None
        registry = _OpenAIToolRegistry(Path(request.cwd), llm.features)
        messages: list[dict[str, object]] = [
            {"role": "user", "content": request.prompt}
        ]
        deadline = time.monotonic() + max(0.001, request.timeout_s)
        token_usage = 0
        token_usage_details: dict[str, int] = {}
        tool_call_count = 0
        tool_rounds = 0
        max_tool_rounds = _feature_int(
            llm.features,
            "max_tool_rounds",
            default=8,
            minimum=1,
            maximum=32,
        )

        while True:
            turn_number = tool_rounds + 1
            payload = self._chat_payload(
                messages,
                prompt_metadata,
                streaming=streaming,
                tools=registry.openai_tools(),
            )
            _progress(
                "turn "
                f"{turn_number}/{max_tool_rounds + 1}: request "
                f"model={payload.get('model')} "
                f"stream={str(streaming).lower()} "
                f"tools={len(payload.get('tools', []))}"
            )
            turn_or_result = self._post_chat_turn(payload, request, deadline, streaming)
            if isinstance(turn_or_result, CliRunResult):
                _progress(
                    "turn "
                    f"{turn_number}/{max_tool_rounds + 1}: "
                    f"failed code={turn_or_result.exit_code} "
                    f"reason={turn_or_result.metadata.get('provider_error_code')}"
                )
                return turn_or_result
            turn = turn_or_result
            _progress(
                "turn "
                f"{turn_number}/{max_tool_rounds + 1}: response "
                f"finish_reason={turn.finish_reason or 'unknown'} "
                f"tool_calls={len(turn.tool_calls)} "
                f"tokens={turn.token_usage}"
            )
            token_usage += turn.token_usage
            if turn.token_usage_details:
                token_usage_details = _merge_token_usage_details(
                    token_usage_details,
                    turn.token_usage_details,
                )
            if turn.tool_calls:
                tool_rounds += 1
                if tool_rounds > max_tool_rounds:
                    return CliRunResult(
                        exit_code=1,
                        stdout="",
                        stderr=(
                            "OpenAI-compatible provider exceeded "
                            f"max_tool_rounds={max_tool_rounds}"
                        ),
                        token_usage=token_usage,
                        metadata={
                            "provider": self.name,
                            "provider_error_code": "tool_round_limit",
                            "tool_call_count": tool_call_count,
                            "tool_rounds": tool_rounds,
                        },
                )
                messages.append(_assistant_tool_message(turn))
                for tool_call in turn.tool_calls:
                    tool_call_count += 1
                    tool_name = _tool_call_name(tool_call)
                    _progress(f"tool {tool_name}: {_tool_call_summary(tool_call)}")
                    tool_message = registry.execute_message(tool_call)
                    _progress(
                        f"tool {tool_name} result: "
                        f"{_tool_result_status(tool_message)}"
                    )
                    messages.append(tool_message)
                continue

            metadata = {
                "provider": self.name,
                "streamed": turn.streamed,
                "http_status": turn.http_status,
                "raw_response_headers": turn.raw_response_headers,
                "finish_reason": turn.finish_reason,
                "token_usage_details": token_usage_details or turn.token_usage_details,
                "raw_response_metadata": turn.raw_response_metadata,
                "reasoning_content_policy": _reasoning_content_policy(llm.features),
                "reasoning_content_observed": turn.reasoning_content_observed,
                "tool_call_count": tool_call_count,
                "tool_rounds": tool_rounds,
            }
            incomplete = _incomplete_finish_reason(metadata["finish_reason"])
            if incomplete:
                _progress(f"final: incomplete finish_reason={incomplete}")
                return _incomplete_generation_result(
                    self.name,
                    turn.text,
                    token_usage,
                    metadata,
                    str(incomplete),
                )
            if turn.text:
                print(turn.text, flush=True)
            _progress(
                "final: "
                f"finish_reason={turn.finish_reason or 'unknown'} "
                f"tokens={token_usage} "
                f"tool_calls={tool_call_count}"
            )
            return CliRunResult(
                exit_code=0,
                stdout=turn.text,
                stderr="",
                token_usage=token_usage,
                metadata=metadata,
            )

    def _chat_payload(
        self,
        messages: list[dict[str, object]],
        prompt_metadata: Mapping[str, object],
        *,
        streaming: bool,
        tools: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        llm = self._config.llm
        payload: dict[str, object] = {
            "model": _metadata_str(prompt_metadata, "model") or llm.model or "",
            "messages": messages,
            "temperature": _metadata_float(prompt_metadata, "temperature", llm.temperature),
        }
        max_tokens = _metadata_int(prompt_metadata, "max_tokens", llm.max_tokens)
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if _feature_enabled(llm.features, "json_mode", default=False):
            payload["response_format"] = {"type": "json_object"}
        reasoning_effort = (
            _metadata_reasoning_effort(prompt_metadata, "reasoning_effort")
            or _metadata_reasoning_effort(prompt_metadata, "effort")
            or _reasoning_effort(llm.features)
        )
        if reasoning_effort:
            payload["reasoning_effort"] = reasoning_effort
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if streaming:
            payload["stream"] = True
            if _feature_enabled(llm.features, "stream_options", default=True):
                payload["stream_options"] = {"include_usage": True}
        return payload

    def _post_chat_turn(
        self,
        payload: dict[str, object],
        request: CliRunRequest,
        deadline: float,
        streaming: bool,
    ) -> "_OpenAICompletionTurn | CliRunResult":
        llm = self._config.llm
        assert llm.base_url is not None
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
        timeout = max(0.001, min(request.timeout_s, deadline - time.monotonic()))
        try:
            with urllib.request.urlopen(http_request, timeout=timeout) as response:
                if streaming and _is_sse_response(response):
                    _progress("stream: connected")
                    return self._read_sse_turn(response, deadline)
                http_status = _http_status(response)
                raw_response_headers = _raw_response_headers(response)
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
                    "raw_response_headers": _raw_response_headers(exc),
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
                return self._read_sse_turn(
                    _BodyResponse(body),
                    time.monotonic() + 60.0,
                )
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
        return _completion_turn_from_parsed(
            parsed,
            streamed=False,
            http_status=http_status,
            raw_response_headers=raw_response_headers,
        )

    def _read_sse_response(self, response: object, deadline: float) -> CliRunResult:
        llm = self._config.llm
        turn_or_result = self._read_sse_turn(response, deadline)
        if isinstance(turn_or_result, CliRunResult):
            return turn_or_result
        turn = turn_or_result
        if turn.tool_calls:
            return _unsupported_tool_calls_result(self.name)
        metadata = {
            "provider": self.name,
            "streamed": True,
            "http_status": turn.http_status,
            "raw_response_headers": turn.raw_response_headers,
            "finish_reason": turn.finish_reason or None,
            "token_usage_details": turn.token_usage_details,
            "raw_response_metadata": turn.raw_response_metadata,
            "reasoning_content_policy": _reasoning_content_policy(llm.features),
            "reasoning_content_observed": turn.reasoning_content_observed,
        }
        incomplete = _incomplete_finish_reason(metadata["finish_reason"])
        if incomplete:
            return _incomplete_generation_result(
                self.name,
                turn.text,
                turn.token_usage,
                metadata,
                str(incomplete),
            )
        if turn.text:
            print(turn.text, flush=True)
        return CliRunResult(
            exit_code=0,
            stdout=turn.text,
            stderr="",
            token_usage=turn.token_usage,
            metadata=metadata,
        )

    def _read_sse_turn(
        self,
        response: object,
        deadline: float,
    ) -> "_OpenAICompletionTurn | CliRunResult":
        http_status = _http_status(response)
        raw_response_headers = _raw_response_headers(response)
        text_parts: list[str] = []
        token_usage = 0
        token_usage_details: dict[str, int] = {}
        finish_reason = ""
        reasoning_content_observed = False
        raw_response_metadata: dict[str, object] = {}
        event_data: list[str] = []
        tool_accumulator = _ToolCallAccumulator()

        def handle_complete_event() -> CliRunResult | None:
            nonlocal event_data
            if not event_data:
                return None
            if not any(item.strip() for item in event_data):
                event_data = []
                return None
            maybe_result = handle_event("\n".join(event_data))
            event_data = []
            return maybe_result

        def handle_event(raw_data: str) -> CliRunResult | None:
            nonlocal token_usage, token_usage_details, finish_reason
            nonlocal raw_response_metadata
            nonlocal reasoning_content_observed
            if _is_done_marker(raw_data):
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
            tool_accumulator.add_event(event)
            event_metadata = _raw_response_metadata(event)
            if event_metadata:
                raw_response_metadata = event_metadata
            event_usage = _token_usage(event)
            if event_usage:
                token_usage = event_usage
            event_usage_details = _token_usage_details(event)
            if event_usage_details:
                token_usage_details = event_usage_details
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
                maybe_result = handle_complete_event()
                if maybe_result is not None:
                    return maybe_result
                continue
            if line.lstrip().startswith(":"):
                continue
            data = _sse_data_payload(line)
            if data is None:
                continue
            if _is_done_marker(data):
                maybe_result = handle_complete_event()
                if maybe_result is not None:
                    return maybe_result
                break
            event_data.append(data)
            if _complete_json_event(event_data):
                maybe_result = handle_complete_event()
                if maybe_result is not None:
                    return maybe_result

        if event_data:
            maybe_result = handle_complete_event()
            if maybe_result is not None:
                return maybe_result

        return _OpenAICompletionTurn(
            text="".join(text_parts),
            finish_reason=finish_reason or None,
            token_usage=token_usage,
            token_usage_details=token_usage_details,
            raw_response_metadata=raw_response_metadata,
            reasoning_content_observed=reasoning_content_observed,
            tool_calls=tool_accumulator.tool_calls(),
            streamed=True,
            http_status=http_status,
            raw_response_headers=raw_response_headers,
        )

    def _read_sse_body(self, body: str) -> CliRunResult:
        return self._read_sse_response(_BodyResponse(body), time.monotonic() + 60.0)


@dataclass
class _OpenAICompletionTurn:
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


class _BodyResponse:
    def __init__(self, text: str) -> None:
        self._lines = iter(text.splitlines(keepends=True))

    def readline(self) -> bytes:
        try:
            return next(self._lines).encode("utf-8")
        except StopIteration:
            return b""


class _ToolCallAccumulator:
    def __init__(self) -> None:
        self._calls: dict[int, dict[str, object]] = {}

    def add_event(self, event: object) -> None:
        choice = _first_choice(event)
        if not choice:
            return
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
                call = self._calls.setdefault(
                    index,
                    {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                call_id = raw_call.get("id")
                if isinstance(call_id, str) and call_id:
                    call["id"] = call_id
                call_type = raw_call.get("type")
                if isinstance(call_type, str) and call_type:
                    call["type"] = call_type
                raw_function = raw_call.get("function")
                if isinstance(raw_function, dict):
                    function = call["function"]
                    if not isinstance(function, dict):
                        function = {"name": "", "arguments": ""}
                        call["function"] = function
                    name = raw_function.get("name")
                    if isinstance(name, str) and name:
                        function["name"] = name
                    arguments = raw_function.get("arguments")
                    if isinstance(arguments, str):
                        function["arguments"] = str(function.get("arguments") or "") + arguments

    def tool_calls(self) -> list[dict[str, object]]:
        return [
            call
            for _, call in sorted(self._calls.items())
            if isinstance(call.get("function"), dict)
            and str(call["function"].get("name") or "")
        ]


@dataclass(frozen=True)
class _OpenAITool:
    name: str
    description: str
    parameters: dict[str, object]


class _OpenAIToolRegistry:
    def __init__(self, cwd: Path, features: dict[str, object]) -> None:
        self._root = cwd.resolve()
        self._features = features

    def openai_tools(self) -> list[dict[str, object]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in self._tools()
        ]

    def execute_message(self, tool_call: dict[str, object]) -> dict[str, object]:
        call_id = str(tool_call.get("id") or "")
        function = tool_call.get("function")
        if not isinstance(function, dict):
            return _tool_result_message(call_id, _tool_error("Malformed tool call"))
        name = str(function.get("name") or "")
        raw_arguments = function.get("arguments")
        if not isinstance(raw_arguments, str):
            raw_arguments = "{}"
        try:
            arguments = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError as exc:
            return _tool_result_message(
                call_id,
                _tool_error(f"Malformed JSON arguments for {name}: {exc}"),
            )
        if not isinstance(arguments, dict):
            return _tool_result_message(
                call_id,
                _tool_error(f"Tool arguments for {name} must be an object"),
            )
        try:
            result = self._execute(name, arguments)
        except Exception as exc:
            result = _tool_error(str(exc))
        return _tool_result_message(call_id, result)

    def _execute(self, name: str, args: dict[str, object]) -> dict[str, object]:
        normalized = name.strip()
        if normalized == "read_file":
            return self._read_file(args)
        if normalized == "write_file":
            return self._write_file(args)
        if normalized == "edit_file":
            return self._edit_file(args)
        if normalized == "list_files":
            return self._list_files(args)
        if normalized == "grep_files":
            return self._grep_files(args)
        if normalized == "fetch_url":
            return self._fetch_url(args)
        if normalized == "web_search":
            return self._web_search(args)
        return _tool_error(f"Unknown tool: {name}")

    def _read_file(self, args: dict[str, object]) -> dict[str, object]:
        path = self._path_arg(args)
        if not path.is_file():
            raise ValueError(f"File is not readable: {path}")
        offset = _int_arg(args, "offset", default=0, minimum=0, maximum=1_000_000)
        limit = _int_arg(args, "limit", default=400, minimum=1, maximum=2_000)
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        selected = lines[offset : offset + limit]
        return {
            "status": "ok",
            "path": self._rel(path),
            "offset": offset,
            "line_count": len(lines),
            "truncated": offset + limit < len(lines),
            "content": "\n".join(
                f"{line_no}: {line}"
                for line_no, line in enumerate(selected, start=offset + 1)
            ),
        }

    def _write_file(self, args: dict[str, object]) -> dict[str, object]:
        path = self._path_arg(args)
        content = args.get("content")
        if not isinstance(content, str):
            raise ValueError("write_file requires string content")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {
            "status": "ok",
            "path": self._rel(path),
            "bytes": len(content.encode("utf-8")),
        }

    def _edit_file(self, args: dict[str, object]) -> dict[str, object]:
        path = self._path_arg(args)
        old = args.get("old_string")
        new = args.get("new_string")
        replace_all = bool(args.get("replace_all", False))
        if not isinstance(old, str) or old == "":
            raise ValueError("edit_file requires non-empty old_string")
        if not isinstance(new, str):
            raise ValueError("edit_file requires string new_string")
        text = path.read_text(encoding="utf-8", errors="replace")
        count = text.count(old)
        if count == 0:
            raise ValueError(f"old_string not found in {self._rel(path)}")
        if count > 1 and not replace_all:
            raise ValueError(
                f"old_string appears {count} times in {self._rel(path)}; set replace_all=true"
            )
        updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        path.write_text(updated, encoding="utf-8")
        return {
            "status": "ok",
            "path": self._rel(path),
            "replacements": count if replace_all else 1,
        }

    def _list_files(self, args: dict[str, object]) -> dict[str, object]:
        pattern = _str_arg(args, "pattern", default="**/*")
        self._validate_relative_pattern(pattern)
        limit = _int_arg(args, "limit", default=200, minimum=1, maximum=2_000)
        files = [
            self._rel(path)
            for path in sorted(self._root.glob(pattern))
            if path.is_file() and self._inside_root(path)
        ]
        return {
            "status": "ok",
            "pattern": pattern,
            "matches": files[:limit],
            "truncated": len(files) > limit,
        }

    def _grep_files(self, args: dict[str, object]) -> dict[str, object]:
        pattern = _str_arg(args, "pattern", default="")
        if not pattern:
            raise ValueError("grep_files requires pattern")
        file_pattern = _str_arg(args, "file_pattern", default="**/*")
        self._validate_relative_pattern(file_pattern)
        max_matches = _int_arg(args, "max_matches", default=100, minimum=1, maximum=1_000)
        regex = re.compile(pattern)
        matches: list[dict[str, object]] = []
        for path in sorted(self._root.glob(file_pattern)):
            if len(matches) >= max_matches:
                break
            if not path.is_file() or not self._inside_root(path):
                continue
            try:
                if path.stat().st_size > 1_000_000:
                    continue
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for line_no, line in enumerate(lines, start=1):
                if regex.search(line):
                    matches.append({
                        "path": self._rel(path),
                        "line": line_no,
                        "text": line,
                    })
                    if len(matches) >= max_matches:
                        break
        return {
            "status": "ok",
            "pattern": pattern,
            "file_pattern": file_pattern,
            "matches": matches,
            "truncated": len(matches) >= max_matches,
        }

    def _fetch_url(self, args: dict[str, object]) -> dict[str, object]:
        url = _str_arg(args, "url", default="")
        if not url:
            raise ValueError("fetch_url requires url")
        _validate_web_url(url)
        max_chars = _int_arg(
            args,
            "max_chars",
            default=20_000,
            minimum=1,
            maximum=100_000,
        )
        timeout_s = _feature_int(
            self._features,
            "web_timeout_s",
            default=10,
            minimum=1,
            maximum=60,
        )
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "EchelonOpenAICompatibleProvider/1.0"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                body = response.read()
                status = _http_status(response)
                headers = _raw_response_headers(response)
        except urllib.error.HTTPError as exc:
            body = exc.read()
            return {
                "status": "error",
                "url": url,
                "http_status": int(exc.code),
                "error": body.decode("utf-8", errors="replace")[:max_chars] or str(exc),
            }
        except (TimeoutError, socket.timeout) as exc:
            return _tool_error(f"fetch_url timed out: {exc}")
        except urllib.error.URLError as exc:
            return _tool_error(f"fetch_url failed: {exc.reason}")
        except OSError as exc:
            return _tool_error(f"fetch_url failed: {exc}")
        text = _decode_web_body(body)
        content = _html_to_text(text)
        return {
            "status": "ok",
            "url": url,
            "http_status": status,
            "headers": headers,
            "content": content[:max_chars],
            "truncated": len(content) > max_chars,
        }

    def _web_search(self, args: dict[str, object]) -> dict[str, object]:
        query = _str_arg(args, "query", default="")
        if not query:
            raise ValueError("web_search requires query")
        max_results = _int_arg(
            args,
            "max_results",
            default=5,
            minimum=1,
            maximum=10,
        )
        search_base = _feature_str(
            self._features,
            "web_search_url",
            default="https://duckduckgo.com/html/",
        )
        _validate_web_url(search_base)
        url = _with_query_param(search_base, "q", query)
        timeout_s = _feature_int(
            self._features,
            "web_timeout_s",
            default=10,
            minimum=1,
            maximum=60,
        )
        try:
            raw_html = _http_get_text(url, timeout_s=timeout_s)
        except ValueError as exc:
            return _tool_error(str(exc))
        except (TimeoutError, socket.timeout) as exc:
            return _tool_error(f"web_search timed out: {exc}")
        except urllib.error.HTTPError as exc:
            return _tool_error(f"web_search failed: HTTP {int(exc.code)}")
        except urllib.error.URLError as exc:
            return _tool_error(f"web_search failed: {exc.reason}")
        except OSError as exc:
            return _tool_error(f"web_search failed: {exc}")
        results = _parse_search_results(raw_html, max_results)
        return {
            "status": "ok",
            "query": query,
            "search_url": url,
            "results": results,
        }

    def _path_arg(self, args: dict[str, object]) -> Path:
        raw = args.get("path") or args.get("filePath") or args.get("file_path")
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("Tool requires path")
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = self._root / candidate
        resolved = candidate.resolve(strict=False)
        if not self._inside_root(resolved):
            raise ValueError(f"Path escapes provider root: {raw}")
        return resolved

    def _inside_root(self, path: Path) -> bool:
        try:
            path.resolve(strict=False).relative_to(self._root)
        except ValueError:
            return False
        return True

    def _rel(self, path: Path) -> str:
        return str(path.resolve(strict=False).relative_to(self._root))

    def _validate_relative_pattern(self, pattern: str) -> None:
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise ValueError(f"Pattern escapes provider root: {pattern}")

    def _tools(self) -> list[_OpenAITool]:
        tools = [
            _OpenAITool(
                "read_file",
                "Read a UTF-8 text file inside the current Echelon provider root.",
                _object_schema({
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 2000},
                }, required=["path"]),
            ),
            _OpenAITool(
                "write_file",
                "Write a UTF-8 text artifact inside the current Echelon provider root.",
                _object_schema({
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                }, required=["path", "content"]),
            ),
            _OpenAITool(
                "edit_file",
                "Replace text in an existing UTF-8 file inside the current provider root.",
                _object_schema({
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                }, required=["path", "old_string", "new_string"]),
            ),
            _OpenAITool(
                "list_files",
                "List files matching a relative glob pattern inside the provider root.",
                _object_schema({
                    "pattern": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 2000},
                }, required=[]),
            ),
            _OpenAITool(
                "grep_files",
                "Search UTF-8 text files with a regular expression inside the provider root.",
                _object_schema({
                    "pattern": {"type": "string"},
                    "file_pattern": {"type": "string"},
                    "max_matches": {"type": "integer", "minimum": 1, "maximum": 1000},
                }, required=["pattern"]),
            ),
        ]
        if _feature_enabled(self._features, "web_tools", default=False):
            tools.extend([
                _OpenAITool(
                    "web_search",
                    "Search the public web and return a small list of result titles and URLs.",
                    _object_schema({
                        "query": {"type": "string"},
                        "max_results": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 10,
                        },
                    }, required=["query"]),
                ),
                _OpenAITool(
                    "fetch_url",
                    "Fetch a public HTTP(S) URL and return bounded readable text.",
                    _object_schema({
                        "url": {"type": "string"},
                        "max_chars": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 100000,
                        },
                    }, required=["url"]),
                ),
            ])
        return tools


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


def _feature_int(
    features: dict[str, object],
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


def _feature_str(
    features: dict[str, object],
    name: str,
    *,
    default: str,
) -> str:
    value = features.get(name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def _prompt_metadata(request: CliRunRequest) -> Mapping[str, object]:
    metadata = request.metadata.get("prompt_metadata")
    return metadata if isinstance(metadata, Mapping) else {}


def _metadata_str(metadata: Mapping[str, object], key: str) -> str:
    value = metadata.get(key)
    return value.strip() if isinstance(value, str) else ""


def _metadata_float(
    metadata: Mapping[str, object],
    key: str,
    default: float,
) -> float:
    value = metadata.get(key)
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _metadata_int(
    metadata: Mapping[str, object],
    key: str,
    default: int | None,
) -> int | None:
    value = metadata.get(key)
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    return default


def _metadata_reasoning_effort(metadata: Mapping[str, object], key: str) -> str:
    normalized = _metadata_str(metadata, key).lower()
    if normalized in {"low", "medium", "high"}:
        return normalized
    return ""


def _reasoning_content_policy(features: dict[str, object]) -> str:
    value = features.get("reasoning_content", "auto")
    if not isinstance(value, str):
        return "auto"
    normalized = value.strip().lower()
    if normalized in {"auto", "field", "merged", "none"}:
        return normalized
    return "auto"


def _reasoning_effort(features: dict[str, object]) -> str:
    value = features.get("reasoning_effort")
    if not isinstance(value, str):
        return ""
    normalized = value.strip().lower()
    if normalized in {"low", "medium", "high"}:
        return normalized
    return ""


def _raw_response_metadata(parsed: object) -> dict[str, object]:
    if not isinstance(parsed, dict):
        return {}
    metadata: dict[str, object] = {}
    for key in ("id", "object", "created", "model", "system_fingerprint"):
        value = parsed.get(key)
        if isinstance(value, (str, int, float, bool)):
            metadata[key] = value
    return metadata


def _http_status(response: object) -> int | None:
    value = getattr(response, "status", None)
    if isinstance(value, int):
        return value
    value = getattr(response, "code", None)
    if isinstance(value, int):
        return value
    getter = getattr(response, "getcode", None)
    if callable(getter):
        code = getter()
        if isinstance(code, int):
            return code
    return None


def _raw_response_headers(response: object) -> dict[str, str]:
    allowlist = {
        "content-type",
        "openai-request-id",
        "request-id",
        "x-request-id",
    }
    headers = getattr(response, "headers", None)
    if headers is None:
        return {}
    items = headers.items() if hasattr(headers, "items") else []
    result: dict[str, str] = {}
    for key, value in items:
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        normalized = key.strip().lower()
        if normalized in allowlist:
            result[normalized] = value.strip()
    return result


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


def _sse_data_payload(line: str) -> str | None:
    stripped = line.lstrip()
    field, separator, value = stripped.partition(":")
    if separator != ":" or field.strip().lower() != "data":
        return None
    if value.startswith(" "):
        value = value[1:]
    return value.strip()


def _is_done_marker(raw_data: str) -> bool:
    normalized = raw_data.strip().strip('"').upper()
    return normalized == "[DONE]"


def _complete_json_event(event_data: list[str]) -> bool:
    if not event_data:
        return False
    raw_data = "\n".join(event_data)
    if _is_done_marker(raw_data):
        return True
    try:
        json.loads(raw_data)
    except json.JSONDecodeError:
        return False
    return True


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


def _completion_turn_from_parsed(
    parsed: object,
    *,
    streamed: bool,
    http_status: int | None,
    raw_response_headers: dict[str, str],
) -> _OpenAICompletionTurn:
    return _OpenAICompletionTurn(
        text=_assistant_text(parsed),
        finish_reason=_finish_reason(parsed),
        token_usage=_token_usage(parsed),
        token_usage_details=_token_usage_details(parsed),
        raw_response_metadata=_raw_response_metadata(parsed),
        reasoning_content_observed=_has_reasoning_content(parsed),
        tool_calls=_message_tool_calls(parsed),
        streamed=streamed,
        http_status=http_status,
        raw_response_headers=raw_response_headers,
    )


def _progress(message: str) -> None:
    print(f"[openai-compatible] {message}", file=sys.stderr, flush=True)


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
    for key in ("path", "filePath", "file_path", "url", "query", "pattern"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return _truncate_progress_value(value)
    return "{}"


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
        return status
    return "unknown"


def _truncate_progress_value(value: str, limit: int = 160) -> str:
    value = " ".join(value.split())
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def _message_tool_calls(parsed: object) -> list[dict[str, object]]:
    if not isinstance(parsed, dict):
        return []
    choices = parsed.get("choices")
    if not isinstance(choices, list) or not choices:
        return []
    first = choices[0]
    if not isinstance(first, dict):
        return []
    message = first.get("message")
    if not isinstance(message, dict):
        return []
    raw_calls = message.get("tool_calls")
    if not isinstance(raw_calls, list):
        return []
    calls: list[dict[str, object]] = []
    for index, raw_call in enumerate(raw_calls):
        if not isinstance(raw_call, dict):
            continue
        function = raw_call.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            arguments = "{}"
        call_id = raw_call.get("id")
        calls.append({
            "id": call_id if isinstance(call_id, str) and call_id else f"call_{index}",
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        })
    return calls


def _assistant_tool_message(turn: _OpenAICompletionTurn) -> dict[str, object]:
    message: dict[str, object] = {
        "role": "assistant",
        "content": turn.text or "",
        "tool_calls": turn.tool_calls,
    }
    return message


def _tool_result_message(call_id: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": json.dumps(payload, sort_keys=True),
    }


def _tool_error(message: str) -> dict[str, object]:
    return {"status": "error", "error": message}


def _object_schema(
    properties: dict[str, object],
    *,
    required: list[str],
) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _str_arg(args: dict[str, object], key: str, *, default: str) -> str:
    value = args.get(key)
    return value if isinstance(value, str) and value else default


def _int_arg(
    args: dict[str, object],
    key: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = args.get(key)
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return max(minimum, min(maximum, value))
    return default


def _merge_token_usage_details(
    existing: dict[str, int],
    incoming: dict[str, int],
) -> dict[str, int]:
    merged = dict(existing)
    for key, value in incoming.items():
        merged[key] = merged.get(key, 0) + value
    return merged


def _validate_web_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"Unsupported URL scheme for web tool: {parsed.scheme}")
    if not parsed.hostname:
        raise ValueError("Web tool URL requires a hostname")
    hostname = parsed.hostname.lower()
    if hostname in {"localhost", "localhost.localdomain"}:
        raise ValueError(f"Web tool URL targets a local hostname: {hostname}")
    try:
        import ipaddress

        address = ipaddress.ip_address(hostname)
    except ValueError:
        return
    if (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
    ):
        raise ValueError(f"Web tool URL targets a non-public address: {hostname}")


def _http_get_text(url: str, *, timeout_s: int) -> str:
    _validate_web_url(url)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "EchelonOpenAICompatibleProvider/1.0"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        return _decode_web_body(response.read())


def _decode_web_body(body: bytes) -> str:
    return body.decode("utf-8", errors="replace")


def _html_to_text(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _parse_search_results(html_text: str, max_results: int) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    anchor_re = re.compile(
        r"(?is)<a\b(?=[^>]*\bclass=[\"'][^\"']*result__a[^\"']*[\"'])"
        r"[^>]*\bhref=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>"
    )
    fallback_re = re.compile(
        r"(?is)<a\b[^>]*\bhref=[\"'](https?://[^\"']+)[\"'][^>]*>(.*?)</a>"
    )
    for regex in (anchor_re, fallback_re):
        for href, title_html in regex.findall(html_text):
            url = _normalize_search_href(href)
            title = _html_to_text(title_html).replace("\n", " ").strip()
            if not url or not title or url in seen:
                continue
            seen.add(url)
            results.append({"title": title, "url": url})
            if len(results) >= max_results:
                return results
    return results


def _normalize_search_href(href: str) -> str:
    href = unescape(href.strip())
    if href.startswith("//"):
        href = "https:" + href
    if href.startswith("/"):
        href = urllib.parse.urljoin("https://duckduckgo.com", href)
    parsed = urllib.parse.urlparse(href)
    query = urllib.parse.parse_qs(parsed.query)
    redirect = query.get("uddg")
    if redirect and redirect[0]:
        href = redirect[0]
    parsed = urllib.parse.urlparse(href)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return href


def _with_query_param(url: str, key: str, value: str) -> str:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(k, v) for k, v in query if k != key]
    query.append((key, value))
    return urllib.parse.urlunparse(
        parsed._replace(query=urllib.parse.urlencode(query))
    )


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


def _incomplete_finish_reason(finish_reason: object) -> str:
    if not isinstance(finish_reason, str):
        return ""
    normalized = finish_reason.strip().lower()
    return normalized if normalized in {"length", "content_filter"} else ""


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
        for container in (message, delta):
            if isinstance(container, dict) and _reasoning_text(container):
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
    choice = _first_choice(event)
    if not choice:
        return ""
    delta = choice.get("delta")
    if isinstance(delta, dict):
        content = _content_text(delta.get("content"))
        if content:
            return content
    message = choice.get("message")
    if isinstance(message, dict):
        content = _content_text(message.get("content"))
        if content:
            return content
    return _content_text(choice.get("text"))


def _event_reasoning_content(event: object) -> str:
    choice = _first_choice(event)
    if not choice:
        return ""
    delta = choice.get("delta")
    if isinstance(delta, dict):
        reasoning = _reasoning_text(delta)
        if reasoning:
            return reasoning
    message = choice.get("message")
    if isinstance(message, dict):
        reasoning = _reasoning_text(message)
        if reasoning:
            return reasoning
    return _reasoning_text(choice)


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
    choice = _first_choice(event)
    if not choice:
        return None
    delta = choice.get("delta")
    return delta if isinstance(delta, dict) else None


def _first_choice(event: object) -> dict[str, object] | None:
    if not isinstance(event, dict):
        return None
    choices = event.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    return first


def _reasoning_text(container: dict[str, object]) -> str:
    for key in (
        "reasoning_content",
        "reasoning",
        "reasoning_text",
        "reasoning_output",
    ):
        value = container.get(key)
        if isinstance(value, str) and value:
            return value
    details = container.get("reasoning_details")
    if isinstance(details, list):
        parts: list[str] = []
        for item in details:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content") or item.get("reasoning")
                if isinstance(text, str):
                    parts.append(text)
        if parts:
            return "".join(parts)
    return ""


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


def _token_usage_details(parsed: object) -> dict[str, int]:
    if not isinstance(parsed, dict):
        return {}
    usage = parsed.get("usage")
    if not isinstance(usage, dict):
        return {}
    details: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            details[key] = value
    if "total_tokens" not in details:
        prompt = details.get("prompt_tokens")
        completion = details.get("completion_tokens")
        if prompt is not None and completion is not None:
            details["total_tokens"] = prompt + completion
    return details


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


def _incomplete_generation_result(
    provider: str,
    stdout: str,
    token_usage: int,
    metadata: dict[str, object],
    finish_reason: str,
) -> CliRunResult:
    enriched = dict(metadata)
    enriched["provider"] = provider
    enriched["provider_error_code"] = "incomplete_generation"
    return CliRunResult(
        exit_code=1,
        stdout=stdout,
        stderr=(
            "OpenAI-compatible response ended before a complete artifact could be "
            f"trusted: finish_reason={finish_reason}"
        ),
        token_usage=token_usage,
        metadata=enriched,
    )
