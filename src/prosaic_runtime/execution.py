"""Shared bounded conversation loop; adapters own encoding and HTTP only."""
import json
import time
from collections.abc import Mapping
from pathlib import Path
from .types import Invocation as CliRunRequest, Result as CliRunResult
from .events import print, emit
from .events import Cancelled, emit_tool_completion, released_text, text_withheld
from .tools import ToolDeadlineExceeded
from .terminal import binding
from .terminal import content_digest
from .openai_compatible_compaction import compact_tool_result_messages, ToolResultCompactionStats
from .rule_hooks import RETRY, STOP
from .openai_compatible_progress import (_progress, _progress_llm_preview, _progress_turn_summary, _single_line_preview, _tool_call_name, _tool_result_status, _elapsed_s)


class ExecutionBackend:
    def _run_prompt_with_tools(
        self,
        request: CliRunRequest,
        prompt_metadata: Mapping[str, object],
        streaming: bool,
        *, initial_messages=None, execution_options=None, turn_capture=None,
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
            Path(request.cwd) if request.cwd is not None else None,
            llm.features,
            prompt_metadata,
        )
        if initial_messages is not None:
            from .openai_compatible_transcript import ProviderTranscript
            transcript = ProviderTranscript(None)
        else:
            transcript = self.open_transcript(request)
        messages: list[dict[str, object]] = initial_messages if initial_messages is not None else [
            {"role": "system", "content": self.tool_guidance()},
            {"role": "user", "content": request.prompt}
        ]
        from .tool_event_contract import current_registry
        evidence_registry = current_registry()
        citation_policy = evidence_registry.bindings.citations if evidence_registry is not None else None
        if citation_policy is not None:
            from .citations import CITATION_INSTRUCTIONS
            messages = list(messages)
            messages.insert(0, {'role': 'system', 'content': CITATION_INSTRUCTIONS})
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
        finalization_forced = False
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
        finalize_after = llm.features.get('finalize_after_tool_rounds')
        if 'finalize_after_tool_rounds' in llm.features and (
            type(finalize_after) is not int or not 1 <= finalize_after <= max_tool_rounds
        ):
            raise ValueError('finalize_after_tool_rounds must be an integer between 1 and max_tool_rounds')
        run_started = time.monotonic()
        # Rule hooks exist only for admitted conversation turns with bound rules.
        hooks = getattr(self, 'rule_hooks', None)

        def rule_stop():
            return CliRunResult(1, '', 'rule violation', token_usage=token_usage, metadata={
                'provider': self.name, 'failure_reason': 'ruleViolation', 'tool_call_count': tool_call_count,
                'tool_rounds': tool_rounds, 'token_usage_details': token_usage_details,
                **_transcript_metadata(transcript)})

        while True:
            turn_number = tool_rounds + 1
            turn_started = time.monotonic()
            initial_tool = _metadata_str(prompt_metadata, 'initial_tool')
            acquiring = turn_number == 1 and 'final_prompt' in prompt_metadata
            def construct_payload():
                if execution_options is not None and execution_options.get('compact_tool_results') is False:
                    payload_messages, compaction = list(messages), ToolResultCompactionStats()
                else:
                    payload_messages, compaction = compact_tool_result_messages(messages, llm.features)
                payload = self._chat_payload(payload_messages, prompt_metadata,
                    streaming=streaming, tools=[] if tools_disabled else registry.openai_tools())
                if acquiring:
                    payload.pop('response_format', None)
                    payload['tools'] = [tool for tool in payload.get('tools', [])
                                        if tool['function']['name'] in prompt_metadata['acquisition_tools']]
                if turn_number == 1 and initial_tool and payload.get('tools'):
                    payload['tool_choice'] = {'type': 'function', 'function': {'name': initial_tool}}
                return payload, compaction
            guard = getattr(self, 'context_guard', None)
            payload, compaction = (guard.construct_native(construct_payload) if guard is not None
                                   else construct_payload())
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
                if finalization_forced:
                    return CliRunResult(
                        exit_code=1, stdout='',
                        stderr='model requested tools during final-answer phase',
                        token_usage=token_usage,
                        metadata={
                            'provider': self.name,
                            'provider_error_code': 'tool_finalization_not_honored',
                            'failure_reason': 'tool_finalization_not_honored',
                            'tool_call_count': tool_call_count,
                            'tool_rounds': tool_rounds,
                            'token_usage_details': token_usage_details,
                            **_transcript_metadata(transcript),
                        },
                    )
                if initial_messages is not None or getattr(registry, 'terminal_tools', ()):
                    if turn.finish_reason not in {'tool_calls', 'tool_use'}:
                        return CliRunResult(1, '', 'incomplete tool batch', token_usage=token_usage,
                            metadata={'provider': self.name, 'streamed': turn.streamed,
                                      'failure_reason': 'incomplete_response',
                                      'token_usage_details': token_usage_details})
                    # No model field may opt into or out of this boundary.
                    turn.tool_calls = list(registry.validate_batch(turn.tool_calls))
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
                # Whole-batch rule admission precedes history, wire and effects,
                # so all three record the arguments that actually run.
                if hooks is not None and hooks.before_tools(turn, registry) is STOP:
                    return rule_stop()
                if turn_capture is not None:
                    turn_capture.assistant(turn)
                messages.append(_assistant_tool_message(turn_capture.wire_turn(turn) if turn_capture is not None else turn))
                tool_signature = _tool_round_signature(turn.tool_calls)
                if tool_signature == previous_tool_signature:
                    identical_tool_rounds += 1
                else:
                    previous_tool_signature = tool_signature
                    identical_tool_rounds = 1
                tool_started = time.monotonic()
                registry.turn = turn_number
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
                    from .tool_event_contract import managed_events
                    managed_event = managed_events(getattr(registry, 'custom', {}).get(tool_name))
                    if not managed_event:
                        emit("tool_started", name=tool_name, call_id=call_id, turn=turn_number,
                             **self.tool_event_metadata(tool_name),
                             **({'argumentsSha256': content_digest(json.loads(tool_call['function']['arguments']))}
                                if getattr(registry, 'terminal_tools', ()) else {}))
                    from .tool_effects import ToolEffectFailure
                    from .tool_outcomes import ToolOutcome
                    refusal = hooks.refusal(tool_call) if hooks is not None else None
                    try:
                        tool_message = (registry.refuse_message(tool_call, refusal) if refusal is not None
                                        else registry.execute_message(tool_call))
                    except (Cancelled, ToolDeadlineExceeded):
                        terminal = getattr(registry, 'terminal_capture', None)
                        if terminal is not None and terminal.evidence is not None:
                            emit_tool_completion(name=tool_name, call_id=call_id, turn=turn_number,
                                status='ok', duration_ms=round((time.monotonic()-call_started)*1000, 3),
                                read_receipts=[], tool_outcome=ToolOutcome('ok').to_dict(),
                                terminalCompletion=binding(terminal.evidence), **self.tool_event_metadata(tool_name))
                        raise
                    except ToolEffectFailure:
                        outcome = getattr(registry, 'last_outcome', ToolOutcome('unclassified')).to_dict()
                        try:
                            if not managed_event:
                                emit('tool_completed', name=tool_name, call_id=call_id, turn=turn_number,
                                     status='error', duration_ms=round((time.monotonic()-call_started)*1000, 3),
                                     read_receipts=[], tool_outcome=outcome, **self.tool_event_metadata(tool_name))
                        except Cancelled:
                            # Terminal journal uncertainty takes precedence over a
                            # later cancellation boundary; neither permits replay.
                            pass
                        raise
                    executed = tool_message
                    from .tool_event_contract import current_registry
                    evidence_registry = current_registry()
                    checked_event = None
                    try:
                        if hooks is not None and refusal is None:
                            # Raw completion remains private; guards check the logical data.
                            tool_message = hooks.after_tool(tool_call, executed)
                        registered = getattr(registry, 'custom', {}).get(tool_name)
                        if evidence_registry is not None and tool_message is not STOP:
                            checked_payload = json.loads(tool_message['content'])
                            if registered is not None and registered.result_contract is not None and refusal is None:
                                from .tool_evidence import digest
                                checked_payload = evidence_registry.stage(registered, json.loads(executed['content']),
                                    checked_payload, call_id=registry.last_contract_call_id,
                                    call_index=registry.last_contract_call_index,
                                    arguments_sha256=digest(json.loads(tool_call['function']['arguments'])),
                                    replayed=registry.last_outcome.replayed)
                                tool_message = {**tool_message, 'content': json.dumps(checked_payload, ensure_ascii=False, allow_nan=False)}
                            checked_event = checked_payload
                    finally:
                        if evidence_registry is not None:
                            evidence_registry.finish_event(getattr(registry, 'last_contract_call_id', None), checked_event)
                    if turn_capture is not None and tool_message is not STOP:
                        turn_capture.tool(tool_call, tool_message)
                    tool_payload = json.loads(executed["content"])
                    from .conformance import _record_tool_result
                    _record_tool_result(tool_name, tool_payload)
                    status = tool_payload.get("status", "unknown")
                    read_receipts = []
                    if tool_name == "read_file" and status == "ok":
                        read_receipts = [{key: tool_payload[key] for key in
                                          ("path", "sha256", "offset", "lines_read", "line_count")}]
                    terminal = getattr(registry, 'terminal_capture', None)
                    completion = terminal.evidence if terminal is not None else None
                    if not managed_event:
                        emit("tool_completed", name=tool_name, call_id=call_id, turn=turn_number,
                             status=status, duration_ms=round((time.monotonic() - call_started) * 1000, 3),
                             read_receipts=read_receipts,
                             tool_outcome=getattr(registry, 'last_outcome', ToolOutcome('ok' if status=='ok' else 'unclassified')).to_dict(),
                             **self.tool_event_metadata(tool_name),
                             **({'terminalCompletion': binding(completion)} if completion is not None else {}))
                    if completion is not None:
                        self.check_boundary()
                        return CliRunResult(0, json.dumps(tool_payload['result'], ensure_ascii=False,
                            allow_nan=False), '', token_usage=token_usage,
                            metadata={'provider': self.name, 'finish_reason': turn.finish_reason,
                                'completion_source': 'host', 'tool_call_count': tool_call_count,
                                'tool_rounds': tool_rounds, 'token_usage_details': token_usage_details,
                                **_transcript_metadata(transcript)})
                    if hooks is not None and tool_message is STOP:
                        return rule_stop()
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
                    messages.append(turn_capture.wire_tool(tool_message) if turn_capture is not None else tool_message)
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
                if finalize_after is not None and tool_rounds >= finalize_after:
                    tools_disabled = True
                    finalization_forced = True
                    messages.append({'role': 'system', 'content':
                        'The tool phase has ended. Return the requested final answer using '
                        'the information available. Do not request any more tools.'})
                elif identical_tool_rounds >= max_identical_tool_rounds:
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
            if hooks is not None:
                decision = hooks.answer(turn, messages)
                if decision is STOP:
                    return rule_stop()
                if decision == RETRY:
                    continue  # Draft and unprivileged feedback were appended; budgets still apply.
            if citation_policy is not None:
                from .citations import citation_markers, validate_citations, CitationViolation
                eligible = evidence_registry.eligible()
                try:
                    markers = citation_markers(turn.text, maximum=evidence_registry.bindings.limits.max_citations)
                    for evidence_id in dict.fromkeys(m['evidenceId'] for m in markers):
                        if evidence_id in eligible.ids:
                            evidence_registry.authorize(evidence_id, 'cite')
                    evidence_registry.citations = validate_citations(turn.text, eligible, citation_policy,
                        limits=evidence_registry.bindings.limits)
                except CitationViolation:
                    if hooks is not None and hooks.citation_retry(turn, messages):
                        continue
                    return CliRunResult(1, '', 'citationViolation', token_usage=token_usage,
                        metadata={'failure_reason': 'citationViolation', 'token_usage_details': token_usage_details})
            if turn_capture is not None:
                turn_capture.assistant(turn)
            if text_withheld():
                # Every answer-stage rule accepted this text. Deliver it as an untagged turn would:
                # streamed text as deltas, each small enough for one event, then the console preview
                # the provider's own preview would have shown (or the plain print when there is none).
                with released_text():
                    if turn.text and turn.streamed:
                        limit = turn_capture.limits.max_event_bytes if turn_capture is not None else 65_536
                        size = max(1, (limit - 128) // 6)  # A character encodes to at most 6 JSON bytes.
                        for start in range(0, len(turn.text), size):
                            emit('text_delta', text=turn.text[start:start + size])
                    from .openai_compatible import _feature_enabled

                    # OpenAI streams preview only with stream_preview on; without it the answer is printed.
                    previews = not turn.streamed or _feature_enabled(llm.features, 'stream_preview', default=True)
                    operator_previewed = turn.previewed or (
                        previews and bool(turn.text) and _progress_llm_preview(turn.text))
                    if turn.text and not operator_previewed:
                        print(turn.text, flush=True)
            elif turn.text and not operator_previewed:
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

