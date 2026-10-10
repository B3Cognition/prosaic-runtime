"""Shared bounded conversation loop; adapters own encoding and HTTP only."""
import json
import time
from collections.abc import Mapping
from pathlib import Path
from .types import Invocation as CliRunRequest, Result as CliRunResult
from .events import print, emit
from .openai_compatible_compaction import compact_tool_result_messages
from .openai_compatible_progress import (_progress, _progress_llm_preview, _progress_turn_summary, _single_line_preview, _tool_call_name, _tool_result_status, _elapsed_s)


class ExecutionBackend:
    def _run_prompt_with_tools(
        self,
        request: CliRunRequest,
        prompt_metadata: Mapping[str, object],
        streaming: bool,
    ) -> CliRunResult:
        from .openai_compatible import (
            _NO_PROGRESS_FINAL_GUIDANCE,
            _assistant_tool_message,
            _feature_int,
            _incomplete_finish_reason,
            _incomplete_generation_result,
            _merge_token_usage_details,
            _metadata_str,
            _reasoning_content_policy,
            _tool_round_signature,
            _transcript_metadata,
        )
        llm = self._config
        assert llm.base_url is not None
        assert llm.model is not None
        registry = self.make_registry(
            Path(request.cwd),
            llm.features,
            prompt_metadata,
        )
        transcript = self.open_transcript(request)
        messages: list[dict[str, object]] = [
            {"role": "system", "content": self.tool_guidance()},
            {"role": "user", "content": request.prompt}
        ]
        deadline = time.monotonic() + max(0.001, request.timeout_s)
        token_usage = 0
        token_usage_details: dict[str, int] = {}
        tool_call_count = 0
        tool_rounds = 0
        tool_result_compactions = 0
        tool_result_compaction_saved_chars = 0
        previous_tool_signature: str | None = None
        identical_tool_rounds = 0
        tools_disabled = False
        tool_no_progress_forced = False
        max_tool_rounds = _feature_int(
            llm.features,
            "max_tool_rounds",
            default=24,
            minimum=1,
            maximum=64,
        )
        max_identical_tool_rounds = _feature_int(
            llm.features,
            "max_identical_tool_rounds",
            default=3,
            minimum=2,
            maximum=10,
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
                tools=[] if tools_disabled else registry.openai_tools(),
            )
            initial_tool = _metadata_str(prompt_metadata, 'initial_tool')
            acquiring = turn_number == 1 and 'final_prompt' in prompt_metadata
            if acquiring:
                payload.pop('response_format', None)
                payload['tools'] = [tool for tool in payload.get('tools', [])
                                    if tool['function']['name'] in prompt_metadata['acquisition_tools']]
            if turn_number == 1 and initial_tool and payload.get('tools'):
                payload['tool_choice'] = {'type': 'function', 'function': {'name': initial_tool}}
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
            if turn_number == 1 and initial_tool and payload.get('tools'):
                names = [_tool_call_name(call) for call in turn.tool_calls]
                if names != [initial_tool]:
                    transcript.write('final', finish_reason='tool_choice_not_honored',
                                     expected_tool=initial_tool, token_usage=token_usage,
                                     tool_call_count=0, tool_rounds=0)
                    return CliRunResult(
                        exit_code=1, stdout='',
                        stderr=f'endpoint did not honor explicit first-tool choice: {initial_tool}',
                        token_usage=token_usage,
                        metadata={
                            'provider': self.name,
                            'request_model': str(payload.get('model') or ''),
                            'provider_error_code': 'tool_choice_not_honored',
                            'failure_reason': 'tool_choice_not_honored',
                            'expected_tool': initial_tool,
                            'observed_tool_call_count': len(names),
                            'tool_call_count': 0, 'tool_rounds': 0,
                            'streamed': turn.streamed,
                            'finish_reason': turn.finish_reason,
                            'http_status': turn.http_status,
                            'token_usage_details': token_usage_details,
                            **_transcript_metadata(transcript),
                        },
                    )
            if turn.tool_calls:
                if tools_disabled:
                    return CliRunResult(
                        exit_code=1,
                        stdout="",
                        stderr="model requested tools after no-progress tool shutdown",
                        token_usage=token_usage,
                        metadata={
                            "provider": self.name,
                            "provider_error_code": "tool_no_progress",
                            "tool_call_count": tool_call_count,
                            "tool_rounds": tool_rounds,
                            "tool_no_progress_forced": True,
                            **_transcript_metadata(transcript),
                        },
                    )
                tool_rounds += 1
                if tool_rounds > max_tool_rounds:
                    last_tool_call = turn.tool_calls[-1]
                    last_tool_name = _tool_call_name(last_tool_call)
                    last_tool_summary = self.tool_call_summary(last_tool_call)
                    last_model_preview = _single_line_preview(turn.text)
                    failure_detail = (
                        f"{self.name} provider exceeded "
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
                tool_signature = _tool_round_signature(turn.tool_calls)
                if tool_signature == previous_tool_signature:
                    identical_tool_rounds += 1
                else:
                    previous_tool_signature = tool_signature
                    identical_tool_rounds = 1
                tool_started = time.monotonic()
                for tool_call in turn.tool_calls:
                    tool_call_count += 1
                    tool_name = _tool_call_name(tool_call)
                    tool_summary = self.tool_call_summary(tool_call)
                    _progress(f"tool {tool_name}: {tool_summary}")
                    transcript.write(
                        "tool_call",
                        turn=turn_number,
                        tool_name=tool_name,
                        tool_summary=tool_summary,
                    )
                    call_id = str(tool_call.get("id") or "")
                    call_started = time.monotonic()
                    emit("tool_started", name=tool_name, call_id=call_id, turn=turn_number,
                         **self.tool_event_metadata(tool_name))
                    tool_message = registry.execute_message(tool_call)
                    tool_payload = json.loads(tool_message["content"])
                    from .conformance import _record_tool_result
                    _record_tool_result(tool_name, tool_payload)
                    status = tool_payload.get("status", "unknown")
                    read_receipts = []
                    if tool_name == "read_file" and status == "ok":
                        read_receipts = [{key: tool_payload[key] for key in
                                          ("path", "sha256", "offset", "lines_read", "line_count")}]
                    emit("tool_completed", name=tool_name, call_id=call_id, turn=turn_number,
                         status=status, duration_ms=round((time.monotonic() - call_started) * 1000, 3),
                         read_receipts=read_receipts, **self.tool_event_metadata(tool_name))
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
                    if acquiring and status != 'ok':
                        return CliRunResult(
                            exit_code=1, stdout='', stderr='required acquisition tool failed',
                            token_usage=token_usage, metadata={
                                'provider': self.name, 'failure_reason': 'acquisition_failed',
                                'provider_error_code': 'acquisition_failed', 'tool_call_count': tool_call_count,
                                'tool_rounds': tool_rounds, 'token_usage_details': token_usage_details,
                                **_transcript_metadata(transcript)})
                if acquiring:
                    messages.append({'role': 'user', 'content': prompt_metadata['final_prompt']})
                    emit('acquisition_completed', tool=initial_tool, turn=turn_number)
                if identical_tool_rounds >= max_identical_tool_rounds:
                    tools_disabled = True
                    tool_no_progress_forced = True
                    initial_guidance = messages[0].get("content")
                    assert isinstance(initial_guidance, str)
                    messages[0]["content"] = (
                        f"{initial_guidance}\n\n{_NO_PROGRESS_FINAL_GUIDANCE}"
                    )
                    _progress(
                        "tool no-progress: repeated identical round "
                        f"{identical_tool_rounds}/{max_identical_tool_rounds}; "
                        "tools disabled for final response"
                    )
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
                "request_model": str(payload.get("model") or ""),
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
                "tool_no_progress_forced": tool_no_progress_forced,
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

