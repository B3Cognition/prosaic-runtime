from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from harness.ai_cli_backend import CliRunRequest, CliRunResult
from harness.ai_cli_backends.openai_compatible_compaction import (
    compact_tool_result_messages,
)
from harness.ai_cli_backends.openai_compatible_filters import OpenAIPathFilter
from harness.ai_cli_backends.openai_compatible_progress import (
    OpenAIStreamPreview,
    _elapsed_s,
    _event_tool_call_delta_summaries,
    _progress,
    _progress_llm_preview,
    _progress_turn_summary,
    _single_line_preview,
    _tool_call_name,
    _tool_call_summary,
    _tool_result_status,
)
from harness.ai_cli_backends.openai_compatible_transcript import (
    open_provider_transcript,
)
from harness.config import HarnessConfig


_OPENAI_COMPATIBLE_TOOL_GUIDANCE = (
    "Prefer bulk context tools first when inspecting Echelon RE or artifact runs. "
    "Use read_re_analysis_pack for run-level context, read_domain_pack for one "
    "source/domain, codegraph_context or perlgraph_context for graph summaries, "
    "grep_context for search with surrounding lines, read_many_files for known "
    "file sets, and list_tree_with_sizes before broad file reads. Keep tool calls "
    "purposeful and return the final artifact once enough evidence is available. "
    "The `echelon_result` control payload is final YAML response text, never a tool or "
    "function call. Once artifacts are complete, stop calling tools and emit that block."
)


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
        registry = _OpenAIToolRegistry(
            Path(request.cwd),
            llm.features,
            prompt_metadata,
        )
        transcript = open_provider_transcript(
            Path(request.cwd),
            llm.features,
            request.metadata,
        )
        messages: list[dict[str, object]] = [
            {"role": "system", "content": _OPENAI_COMPATIBLE_TOOL_GUIDANCE},
            {"role": "user", "content": request.prompt}
        ]
        deadline = time.monotonic() + max(0.001, request.timeout_s)
        token_usage = 0
        token_usage_details: dict[str, int] = {}
        tool_call_count = 0
        tool_rounds = 0
        tool_result_compactions = 0
        tool_result_compaction_saved_chars = 0
        max_tool_rounds = _feature_int(
            llm.features,
            "max_tool_rounds",
            default=24,
            minimum=1,
            maximum=64,
        )
        run_started = time.monotonic()

        while True:
            turn_number = tool_rounds + 1
            turn_started = time.monotonic()
            payload_messages, compaction = compact_tool_result_messages(
                messages,
                llm.features,
            )
            if compaction.compacted:
                tool_result_compactions += compaction.compacted
                tool_result_compaction_saved_chars += compaction.saved_chars
                _progress(
                    "compaction: "
                    f"tool_results={compaction.tool_results} "
                    f"compacted={compaction.compacted} "
                    f"saved_chars={compaction.saved_chars}"
                )
                transcript.write(
                    "compaction",
                    turn=turn_number,
                    tool_results=compaction.tool_results,
                    compacted=compaction.compacted,
                    saved_chars=compaction.saved_chars,
                )
            payload = self._chat_payload(
                payload_messages,
                prompt_metadata,
                streaming=streaming,
                tools=registry.openai_tools(),
            )
            _progress(
                "turn "
                f"{turn_number}: request "
                f"model={payload.get('model')} "
                f"stream={str(streaming).lower()} "
                f"messages={len(messages)} "
                f"tools={len(payload.get('tools', []))} "
                f"tool_rounds={tool_rounds}/{max_tool_rounds}"
            )
            turn_or_result = self._post_chat_turn(payload, request, deadline, streaming)
            if isinstance(turn_or_result, CliRunResult):
                transcript.write(
                    "provider_error",
                    turn=turn_number,
                    exit_code=turn_or_result.exit_code,
                    reason=str(turn_or_result.metadata.get("provider_error_code") or ""),
                )
                turn_or_result.metadata.update(_transcript_metadata(transcript))
                _progress(
                    "turn "
                    f"{turn_number}: "
                    f"failed code={turn_or_result.exit_code} "
                    f"reason={turn_or_result.metadata.get('provider_error_code')}"
                )
                return turn_or_result
            turn = turn_or_result
            model_elapsed = time.monotonic() - turn_started
            transcript.write(
                "turn_response",
                turn=turn_number,
                finish_reason=turn.finish_reason or "",
                tool_calls=len(turn.tool_calls),
                text_chars=len(turn.text),
                token_usage=turn.token_usage,
                streamed=turn.streamed,
                model_time_ms=int(model_elapsed * 1000),
            )
            _progress(
                "turn "
                f"{turn_number}: response "
                f"finish_reason={turn.finish_reason or 'unknown'} "
                f"tool_calls={len(turn.tool_calls)} "
                f"text_chars={len(turn.text)} "
                f"tokens={turn.token_usage} "
                f"elapsed={_elapsed_s(turn_started)}s"
            )
            operator_previewed = turn.previewed
            if turn.text and not turn.streamed:
                operator_previewed = _progress_llm_preview(turn.text)
            token_usage += turn.token_usage
            if turn.token_usage_details:
                token_usage_details = _merge_token_usage_details(
                    token_usage_details,
                    turn.token_usage_details,
                )
            if turn.tool_calls:
                tool_rounds += 1
                if tool_rounds > max_tool_rounds:
                    last_tool_call = turn.tool_calls[-1]
                    last_tool_name = _tool_call_name(last_tool_call)
                    last_tool_summary = _tool_call_summary(last_tool_call)
                    last_model_preview = _single_line_preview(turn.text)
                    failure_detail = (
                        "OpenAI-compatible provider exceeded "
                        f"max_tool_rounds={max_tool_rounds}; "
                        f"last_tool={last_tool_name}"
                    )
                    if last_tool_summary:
                        failure_detail += f" {last_tool_summary}"
                    if last_model_preview:
                        failure_detail += (
                            f"; last_model_preview={last_model_preview}"
                        )
                    _progress(
                        "final: failed reason=tool_round_limit "
                        f"last_tool={last_tool_name}"
                        + (f" {last_tool_summary}" if last_tool_summary else "")
                        + (
                            f" last_model_preview={last_model_preview}"
                            if last_model_preview else ""
                        )
                    )
                    transcript.write(
                        "final",
                        finish_reason="tool_round_limit",
                        token_usage=token_usage,
                        tool_call_count=tool_call_count,
                        tool_rounds=tool_rounds,
                        last_tool_name=last_tool_name,
                        last_tool_summary=last_tool_summary,
                        last_model_preview=last_model_preview,
                        elapsed_ms=int((time.monotonic() - run_started) * 1000),
                    )
                    return CliRunResult(
                        exit_code=1,
                        stdout="",
                        stderr=failure_detail,
                        token_usage=token_usage,
                        metadata={
                            "provider": self.name,
                            "provider_error_code": "tool_round_limit",
                            "tool_call_count": tool_call_count,
                            "tool_rounds": tool_rounds,
                            "last_tool_name": last_tool_name,
                            "last_tool_summary": last_tool_summary,
                            "last_model_preview": last_model_preview,
                            "tool_result_compactions": tool_result_compactions,
                            "tool_result_compaction_saved_chars": (
                                tool_result_compaction_saved_chars
                            ),
                            **_transcript_metadata(transcript),
                        },
                )
                _progress(
                    "tool budget: "
                    f"rounds={tool_rounds}/{max_tool_rounds} "
                    f"calls_total={tool_call_count}"
                )
                messages.append(_assistant_tool_message(turn))
                tool_started = time.monotonic()
                for tool_call in turn.tool_calls:
                    tool_call_count += 1
                    tool_name = _tool_call_name(tool_call)
                    tool_summary = _tool_call_summary(tool_call)
                    _progress(f"tool {tool_name}: {tool_summary}")
                    transcript.write(
                        "tool_call",
                        turn=turn_number,
                        tool_name=tool_name,
                        tool_summary=tool_summary,
                    )
                    tool_message = registry.execute_message(tool_call)
                    tool_result = _tool_result_status(tool_message)
                    _progress(
                        f"tool {tool_name} result: "
                        f"{tool_result}"
                    )
                    transcript.write(
                        "tool_result",
                        turn=turn_number,
                        tool_name=tool_name,
                        tool_result=tool_result,
                    )
                    messages.append(tool_message)
                tool_elapsed = time.monotonic() - tool_started
                transcript.write(
                    "turn_summary",
                    turn=turn_number,
                    model_time_ms=int(model_elapsed * 1000),
                    tool_time_ms=int(tool_elapsed * 1000),
                    model_text_chars=len(turn.text),
                    turn_tool_calls=len(turn.tool_calls),
                    tool_rounds=tool_rounds,
                    max_tool_rounds=max_tool_rounds,
                    tool_call_count=tool_call_count,
                )
                _progress_turn_summary(
                    turn_number,
                    model_elapsed=model_elapsed,
                    tool_elapsed=tool_elapsed,
                    model_text_chars=len(turn.text),
                    turn_tool_calls=len(turn.tool_calls),
                    tool_rounds=tool_rounds,
                    max_tool_rounds=max_tool_rounds,
                    tool_call_count=tool_call_count,
                )
                continue

            transcript.write(
                "turn_summary",
                turn=turn_number,
                model_time_ms=int(model_elapsed * 1000),
                tool_time_ms=0,
                model_text_chars=len(turn.text),
                turn_tool_calls=0,
                tool_rounds=tool_rounds,
                max_tool_rounds=max_tool_rounds,
                tool_call_count=tool_call_count,
            )
            _progress_turn_summary(
                turn_number,
                model_elapsed=model_elapsed,
                tool_elapsed=0.0,
                model_text_chars=len(turn.text),
                turn_tool_calls=0,
                tool_rounds=tool_rounds,
                max_tool_rounds=max_tool_rounds,
                tool_call_count=tool_call_count,
            )
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
                "tool_result_compactions": tool_result_compactions,
                "tool_result_compaction_saved_chars": tool_result_compaction_saved_chars,
                **_transcript_metadata(transcript),
            }
            incomplete = _incomplete_finish_reason(metadata["finish_reason"])
            if incomplete:
                _progress(f"final: incomplete finish_reason={incomplete}")
                transcript.write(
                    "final",
                    finish_reason=str(incomplete),
                    token_usage=token_usage,
                    tool_call_count=tool_call_count,
                    tool_rounds=tool_rounds,
                    elapsed_ms=int((time.monotonic() - run_started) * 1000),
                )
                return _incomplete_generation_result(
                    self.name,
                    turn.text,
                    token_usage,
                    metadata,
                    str(incomplete),
                )
            if turn.text and not operator_previewed:
                print(turn.text, flush=True)
            _progress(
                "final: "
                f"finish_reason={turn.finish_reason or 'unknown'} "
                f"tokens={token_usage} "
                f"tool_calls={tool_call_count} "
                f"elapsed={_elapsed_s(run_started)}s"
            )
            transcript.write(
                "final",
                finish_reason=turn.finish_reason or "",
                token_usage=token_usage,
                tool_call_count=tool_call_count,
                tool_rounds=tool_rounds,
                elapsed_ms=int((time.monotonic() - run_started) * 1000),
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
                    _progress("stream: connected; waiting for model deltas")
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
        if turn.text and not turn.previewed:
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
        progress_detail = _feature_str(
            self._config.llm.features,
            "progress_detail",
            default="normal",
        ).lower()
        stream_debug = progress_detail == "debug"
        stream_preview = OpenAIStreamPreview(
            enabled=_feature_enabled(
                self._config.llm.features,
                "stream_preview",
                default=True,
            ),
            max_chars=_feature_int(
                self._config.llm.features,
                "stream_preview_max_chars",
                default=1200,
                minimum=80,
                maximum=100_000,
            ),
            max_lines=_feature_int(
                self._config.llm.features,
                "stream_preview_max_lines",
                default=12,
                minimum=1,
                maximum=1_000,
            ),
        )

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
            for summary in _event_tool_call_delta_summaries(event):
                if stream_debug:
                    _progress(f"stream: tool_call_delta {summary}")
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
                if stream_debug:
                    _progress(
                        "stream: content_delta "
                        f"chars={len(content)} "
                        f"total_chars={sum(len(part) for part in text_parts)}"
                    )
                stream_preview.append(content)
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
        stream_preview.flush()

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
            previewed=stream_preview.emitted,
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
    previewed: bool = False


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
    def __init__(
        self,
        cwd: Path,
        features: dict[str, object],
        prompt_metadata: Mapping[str, object] | None = None,
    ) -> None:
        self._root = cwd.resolve()
        self._features = features
        self._path_filter = OpenAIPathFilter(self._root, features)
        metadata = prompt_metadata or {}
        self._read_scope_roots = self._scope_paths(metadata, "tool_read_roots")
        self._write_scope_paths = self._scope_paths(metadata, "tool_write_paths")

    def _scope_paths(
        self,
        metadata: Mapping[str, object],
        key: str,
    ) -> tuple[Path, ...]:
        raw_paths = metadata.get(key)
        if not isinstance(raw_paths, list):
            return ()
        paths: list[Path] = []
        for raw in raw_paths:
            if not isinstance(raw, str) or not raw.strip():
                continue
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                candidate = self._root / candidate
            resolved = candidate.resolve(strict=False)
            if not self._inside_root(resolved):
                raise ValueError(f"{key} escapes provider root: {raw}")
            paths.append(resolved)
        return tuple(paths)

    def _inside_read_scope(self, path: Path) -> bool:
        if not self._read_scope_roots:
            return True
        resolved = path.resolve(strict=False)
        return any(
            resolved == root or root in resolved.parents
            for root in self._read_scope_roots
        )

    def _require_read_scope(self, path: Path) -> None:
        if not self._inside_read_scope(path):
            raise ValueError(f"Path is outside dispatch read scope: {self._rel(path)}")

    def _require_write_scope(self, path: Path) -> None:
        if not self._write_scope_paths:
            return
        resolved = path.resolve(strict=False)
        if resolved not in self._write_scope_paths:
            raise ValueError(f"Path is outside dispatch write scope: {self._rel(path)}")

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
        if normalized == "echelon_result":
            return {
                "status": "retry",
                "code": "result_contract_not_tool",
                "instruction": (
                    "echelon_result is not a callable tool. Return it now as the final YAML "
                    "response block. Do not call more tools and do not add prose around it."
                ),
            }
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
        if normalized == "read_many_files":
            return self._read_many_files(args)
        if normalized == "list_tree_with_sizes":
            return self._list_tree_with_sizes(args)
        if normalized == "grep_context":
            return self._grep_context(args)
        if normalized == "read_domain_pack":
            return self._read_domain_pack(args)
        if normalized == "read_re_analysis_pack":
            return self._read_re_analysis_pack(args)
        if normalized == "codegraph_context":
            return self._codegraph_context(args)
        if normalized == "perlgraph_context":
            return self._perlgraph_context(args)
        if normalized == "fetch_url":
            return self._fetch_url(args)
        if normalized == "web_search":
            return self._web_search(args)
        return _tool_error(f"Unknown tool: {name}")

    def _read_file(self, args: dict[str, object]) -> dict[str, object]:
        path = self._path_arg(args)
        self._require_read_scope(path)
        if not path.is_file():
            raise ValueError(f"File is not readable: {path}")
        reason = self._path_filter.reason(path)
        if reason:
            raise ValueError(f"{self._rel(path)} ignored by provider filter: {reason}")
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
        self._require_write_scope(path)
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
        self._require_write_scope(path)
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
            if path.is_file()
            and self._inside_root(path)
            and self._inside_read_scope(path)
            and self._path_filter.visible_file(path)
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
            if (
                not path.is_file()
                or not self._inside_root(path)
                or not self._inside_read_scope(path)
                or not self._path_filter.visible_file(path)
            ):
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

    def _read_many_files(self, args: dict[str, object]) -> dict[str, object]:
        raw_paths = args.get("paths")
        if not isinstance(raw_paths, list) or not raw_paths:
            raise ValueError("read_many_files requires non-empty paths array")
        paths = [
            self._resolve_path_value(raw)
            for raw in raw_paths
            if isinstance(raw, str) and raw.strip()
        ]
        if not paths:
            raise ValueError("read_many_files requires at least one string path")
        if len(paths) > 200:
            paths = paths[:200]
        limit_per_file = _int_arg(
            args,
            "limit_per_file",
            default=500,
            minimum=1,
            maximum=5_000,
        )
        max_total_chars = _int_arg(
            args,
            "max_total_chars",
            default=120_000,
            minimum=1_000,
            maximum=1_000_000,
        )
        remaining = max_total_chars
        files: list[dict[str, object]] = []
        truncated = len(raw_paths) > len(paths)
        for path in paths:
            if remaining <= 0:
                truncated = True
                break
            if not path.is_file():
                files.append({
                    "path": self._rel(path),
                    "status": "error",
                    "error": "not a readable file",
                })
                continue
            if not self._inside_read_scope(path):
                files.append({
                    "path": self._rel(path),
                    "status": "skipped",
                    "error": "outside dispatch read scope",
                })
                continue
            reason = self._path_filter.reason(path)
            if reason:
                files.append({
                    "path": self._rel(path),
                    "status": "skipped",
                    "error": reason,
                })
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError as exc:
                files.append({
                    "path": self._rel(path),
                    "status": "error",
                    "error": str(exc),
                })
                continue
            selected = lines[:limit_per_file]
            content = "\n".join(
                f"{line_no}: {line}"
                for line_no, line in enumerate(selected, start=1)
            )
            file_truncated = len(lines) > limit_per_file or len(content) > remaining
            files.append({
                "path": self._rel(path),
                "status": "ok",
                "line_count": len(lines),
                "truncated": file_truncated,
                "content": content[:remaining],
            })
            truncated = truncated or file_truncated
            remaining -= min(len(content), remaining)
        return {
            "status": "ok",
            "files": files,
            "truncated": truncated,
        }

    def _list_tree_with_sizes(self, args: dict[str, object]) -> dict[str, object]:
        path = self._path_arg(args, default=".")
        self._require_read_scope(path)
        max_entries = _int_arg(
            args,
            "max_entries",
            default=500,
            minimum=1,
            maximum=10_000,
        )
        entries: list[dict[str, object]] = []
        candidates = [path] if path.is_file() else sorted(path.rglob("*"))
        truncated = False
        for candidate in candidates:
            if len(entries) >= max_entries:
                truncated = True
                break
            if not self._inside_root(candidate) or not self._path_filter.visible_tree_entry(candidate):
                continue
            if candidate.is_dir():
                entries.append({
                    "path": self._rel(candidate),
                    "type": "dir",
                    "size": 0,
                })
            elif candidate.is_file():
                try:
                    size = candidate.stat().st_size
                except OSError:
                    size = 0
                entries.append({
                    "path": self._rel(candidate),
                    "type": "file",
                    "size": size,
                })
        return {
            "status": "ok",
            "path": self._rel(path),
            "entries": entries,
            "truncated": truncated,
        }

    def _grep_context(self, args: dict[str, object]) -> dict[str, object]:
        pattern = _str_arg(args, "pattern", default="")
        if not pattern:
            raise ValueError("grep_context requires pattern")
        base = self._path_arg(args, default=".")
        self._require_read_scope(base)
        file_pattern = _str_arg(args, "file_pattern", default="**/*")
        self._validate_relative_pattern(file_pattern)
        before = _int_arg(args, "before", default=2, minimum=0, maximum=20)
        after = _int_arg(args, "after", default=2, minimum=0, maximum=20)
        max_matches = _int_arg(args, "max_matches", default=100, minimum=1, maximum=1_000)
        regex = re.compile(pattern)
        matches: list[dict[str, object]] = []
        paths = [base] if base.is_file() else sorted(base.glob(file_pattern))
        truncated = False
        for path in paths:
            if len(matches) >= max_matches:
                truncated = True
                break
            if (
                not path.is_file()
                or not self._inside_root(path)
                or not self._inside_read_scope(path)
                or not self._path_filter.visible_file(path)
            ):
                continue
            try:
                if path.stat().st_size > 2_000_000:
                    continue
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for index, line in enumerate(lines):
                if not regex.search(line):
                    continue
                start = max(0, index - before)
                end = min(len(lines), index + after + 1)
                context = "\n".join(
                    f"{line_no}: {context_line}"
                    for line_no, context_line in enumerate(
                        lines[start:end],
                        start=start + 1,
                    )
                )
                matches.append({
                    "path": self._rel(path),
                    "line": index + 1,
                    "text": line,
                    "context": context,
                })
                if len(matches) >= max_matches:
                    truncated = True
                    break
        return {
            "status": "ok",
            "pattern": pattern,
            "path": self._rel(base),
            "matches": matches,
            "truncated": truncated,
        }

    def _read_re_analysis_pack(self, args: dict[str, object]) -> dict[str, object]:
        run_dir = self._path_arg(args, key="run_dir")
        self._require_read_scope(run_dir)
        max_chars = _int_arg(
            args,
            "max_chars_per_file",
            default=80_000,
            minimum=1_000,
            maximum=500_000,
        )
        files, missing, truncated = self._read_pack_files(
            run_dir,
            [
                "re-execution-plan.json",
                "re-source-index.json",
                "re-workspace-inputs.json",
                "workspace/domain-catalog.md",
                "workspace/architecture-map.json",
                "workspace/workspace-manifest.json",
                "workspace/repos-manifest.json",
                "workspace/cross-repo.json",
                "analysis.json",
                "re-analysis-manifest.json",
            ],
            max_chars=max_chars,
        )
        return {
            "status": "ok",
            "run_dir": self._rel(run_dir),
            "files": files,
            "missing": missing,
            "truncated": truncated,
        }

    def _read_domain_pack(self, args: dict[str, object]) -> dict[str, object]:
        run_dir = self._path_arg(args, key="run_dir")
        self._require_read_scope(run_dir)
        source_id = _str_arg(args, "source_id", default="")
        domain_id = _str_arg(args, "domain_id", default="")
        if not source_id:
            raise ValueError("read_domain_pack requires source_id")
        if not domain_id:
            raise ValueError("read_domain_pack requires domain_id")
        max_files = _int_arg(args, "max_files", default=200, minimum=1, maximum=2_000)
        max_chars = _int_arg(
            args,
            "max_chars_per_file",
            default=80_000,
            minimum=1_000,
            maximum=500_000,
        )
        source_run_dir = run_dir / "sources" / source_id
        manifest_path = source_run_dir / "domain-manifest.json"
        manifest = self._read_json_file(manifest_path)
        domain_entry = self._domain_entry(manifest, domain_id)
        owned_root = _mapping_str(domain_entry, "root") or _mapping_str(
            domain_entry,
            "owned_root",
        )
        if not owned_root:
            owned_root = _mapping_str(domain_entry, "path") or "."
        source_root = self._source_root_from_index(run_dir, source_id)
        domain_source_root = (source_root / owned_root).resolve(strict=False)
        if not self._inside_root(domain_source_root):
            raise ValueError(f"Domain source root escapes provider root: {owned_root}")
        self._require_read_scope(domain_source_root)
        source_files = self._tree_files(domain_source_root, max_entries=max_files)
        target_spec = self._file_payload(
            source_run_dir / "specs" / domain_id / "spec.md",
            max_chars=max_chars,
        )
        analysis = self._file_payload(source_run_dir / "analysis.json", max_chars=max_chars)
        return {
            "status": "ok",
            "run_dir": self._rel(run_dir),
            "source_id": source_id,
            "domain_id": domain_id,
            "owned_root": owned_root,
            "domain_manifest": self._file_payload(manifest_path, max_chars=max_chars),
            "analysis": analysis,
            "target_spec": target_spec,
            "source_files": source_files,
            "truncated": len(source_files) >= max_files,
        }

    def _codegraph_context(self, args: dict[str, object]) -> dict[str, object]:
        return self._graph_context(
            args,
            [
                "codegraph-summary.json",
                "codegraph-analysis.json",
                "codegraph-index.json",
            ],
        )

    def _perlgraph_context(self, args: dict[str, object]) -> dict[str, object]:
        return self._graph_context(
            args,
            [
                "perlgraph-summary.json",
                "perlgraph-analysis.json",
                "perlgraph-index.json",
            ],
        )

    def _graph_context(
        self,
        args: dict[str, object],
        file_names: list[str],
    ) -> dict[str, object]:
        run_dir = self._path_arg(args, key="run_dir")
        self._require_read_scope(run_dir)
        source_id = _str_arg(args, "source_id", default="")
        if not source_id:
            raise ValueError("graph context tools require source_id")
        max_chars = _int_arg(
            args,
            "max_chars_per_file",
            default=80_000,
            minimum=1_000,
            maximum=500_000,
        )
        files: dict[str, str] = {}
        missing: list[str] = []
        truncated = False
        for file_name in file_names:
            path = run_dir / "sources" / source_id / file_name
            if not path.is_file() or not self._inside_root(path):
                missing.append(file_name)
                continue
            payload = self._file_payload(path, max_chars=max_chars)
            content = payload.get("content")
            if isinstance(content, str):
                files[file_name] = content
            truncated = truncated or bool(payload.get("truncated"))
        return {
            "status": "ok",
            "run_dir": self._rel(run_dir),
            "source_id": source_id,
            "files": files,
            "missing": missing,
            "truncated": truncated,
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

    def _path_arg(
        self,
        args: dict[str, object],
        *,
        key: str = "path",
        default: str | None = None,
    ) -> Path:
        raw = args.get(key)
        if key == "path":
            raw = raw or args.get("filePath") or args.get("file_path")
        return self._resolve_path_value(raw, default=default)

    def _resolve_path_value(
        self,
        raw: object,
        *,
        default: str | None = None,
    ) -> Path:
        if not isinstance(raw, str) or not raw.strip():
            if default is None:
                raise ValueError("Tool requires path")
            raw = default
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = self._root / candidate
        resolved = candidate.resolve(strict=False)
        if not self._inside_root(resolved):
            raise ValueError(f"Path escapes provider root: {raw}")
        return resolved

    def _read_pack_files(
        self,
        base: Path,
        relative_paths: list[str],
        *,
        max_chars: int,
    ) -> tuple[dict[str, str], list[str], bool]:
        files: dict[str, str] = {}
        missing: list[str] = []
        truncated = False
        for relative in relative_paths:
            path = (base / relative).resolve(strict=False)
            if not self._inside_root(path) or not path.is_file():
                missing.append(relative)
                continue
            payload = self._file_payload(path, max_chars=max_chars)
            content = payload.get("content")
            if isinstance(content, str):
                files[relative] = content
            truncated = truncated or bool(payload.get("truncated"))
        return files, missing, truncated

    def _file_payload(self, path: Path, *, max_chars: int) -> dict[str, object]:
        if not self._inside_root(path):
            return {
                "status": "error",
                "path": str(path),
                "error": "path escapes provider root",
            }
        if not path.is_file():
            return {
                "status": "missing",
                "path": self._rel(path),
                "content": "",
                "truncated": False,
            }
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return {
                "status": "error",
                "path": self._rel(path),
                "error": str(exc),
            }
        return {
            "status": "ok",
            "path": self._rel(path),
            "content": content[:max_chars],
            "truncated": len(content) > max_chars,
        }

    def _read_json_file(self, path: Path) -> object:
        if not path.is_file() or not self._inside_root(path):
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _domain_entry(self, manifest: object, domain_id: str) -> Mapping[str, object]:
        if not isinstance(manifest, Mapping):
            return {}
        domains = manifest.get("domains")
        if not isinstance(domains, list):
            return {}
        for item in domains:
            if not isinstance(item, Mapping):
                continue
            item_id = (
                _mapping_str(item, "domain_id")
                or _mapping_str(item, "id")
                or _mapping_str(item, "name")
            )
            if item_id == domain_id:
                return item
        return {}

    def _source_root_from_index(self, run_dir: Path, source_id: str) -> Path:
        index = self._read_json_file(run_dir / "re-source-index.json")
        source_entry = self._source_index_entry(index, source_id)
        raw_path = _mapping_str(source_entry, "absolute_path")
        if not raw_path:
            raw_path = _mapping_str(source_entry, "path")
        if not raw_path:
            raw_path = f"sources/{source_id}"
        return self._resolve_path_value(raw_path)

    def _source_index_entry(self, index: object, source_id: str) -> Mapping[str, object]:
        if not isinstance(index, Mapping):
            return {}
        sources = index.get("sources")
        if not isinstance(sources, list):
            return {}
        for item in sources:
            if not isinstance(item, Mapping):
                continue
            item_id = (
                _mapping_str(item, "id")
                or _mapping_str(item, "source_id")
                or _mapping_str(item, "name")
            )
            if item_id == source_id:
                return item
        return {}

    def _tree_files(self, base: Path, *, max_entries: int) -> list[dict[str, object]]:
        files: list[dict[str, object]] = []
        if not base.exists():
            return files
        candidates = [base] if base.is_file() else sorted(base.rglob("*"))
        for path in candidates:
            if len(files) >= max_entries:
                break
            if (
                not path.is_file()
                or not self._inside_root(path)
                or not self._path_filter.visible_file(path)
            ):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            files.append({"path": self._rel(path), "size": size})
        return files

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
            _OpenAITool(
                "read_many_files",
                "Read multiple UTF-8 files in one call with line numbers and bounded output.",
                _object_schema({
                    "paths": {"type": "array", "items": {"type": "string"}},
                    "limit_per_file": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 5000,
                    },
                    "max_total_chars": {
                        "type": "integer",
                        "minimum": 1000,
                        "maximum": 1000000,
                    },
                }, required=["paths"]),
            ),
            _OpenAITool(
                "list_tree_with_sizes",
                "Recursively list files and directories with byte sizes under a provider-root path.",
                _object_schema({
                    "path": {"type": "string"},
                    "max_entries": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10000,
                    },
                }, required=[]),
            ),
            _OpenAITool(
                "grep_context",
                "Search UTF-8 files and return matching lines with surrounding context.",
                _object_schema({
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "file_pattern": {"type": "string"},
                    "before": {"type": "integer", "minimum": 0, "maximum": 20},
                    "after": {"type": "integer", "minimum": 0, "maximum": 20},
                    "max_matches": {"type": "integer", "minimum": 1, "maximum": 1000},
                }, required=["pattern"]),
            ),
            _OpenAITool(
                "read_domain_pack",
                "Read an RE source/domain context pack: manifest, analysis, target spec, and source file list.",
                _object_schema({
                    "run_dir": {"type": "string"},
                    "source_id": {"type": "string"},
                    "domain_id": {"type": "string"},
                    "max_files": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 2000,
                    },
                    "max_chars_per_file": {
                        "type": "integer",
                        "minimum": 1000,
                        "maximum": 500000,
                    },
                }, required=["run_dir", "source_id", "domain_id"]),
            ),
            _OpenAITool(
                "read_re_analysis_pack",
                "Read high-value RE run-level planning, index, workspace, and catalog files in one call.",
                _object_schema({
                    "run_dir": {"type": "string"},
                    "max_chars_per_file": {
                        "type": "integer",
                        "minimum": 1000,
                        "maximum": 500000,
                    },
                }, required=["run_dir"]),
            ),
            _OpenAITool(
                "codegraph_context",
                "Read available codegraph summary, analysis, and index files for one RE source.",
                _object_schema({
                    "run_dir": {"type": "string"},
                    "source_id": {"type": "string"},
                    "max_chars_per_file": {
                        "type": "integer",
                        "minimum": 1000,
                        "maximum": 500000,
                    },
                }, required=["run_dir", "source_id"]),
            ),
            _OpenAITool(
                "perlgraph_context",
                "Read available perlgraph summary, analysis, and index files for one RE source.",
                _object_schema({
                    "run_dir": {"type": "string"},
                    "source_id": {"type": "string"},
                    "max_chars_per_file": {
                        "type": "integer",
                        "minimum": 1000,
                        "maximum": 500000,
                    },
                }, required=["run_dir", "source_id"]),
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


def _transcript_metadata(transcript: object) -> dict[str, object]:
    path = getattr(transcript, "path", None)
    if isinstance(path, Path):
        return {"provider_transcript_path": str(path)}
    return {}


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


def _mapping_str(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    return item.strip() if isinstance(item, str) else ""


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
