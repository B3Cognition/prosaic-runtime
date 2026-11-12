"""Public Prosaic-only invocation API."""
from dataclasses import replace
from contextlib import contextmanager, nullcontext
from pathlib import Path
import os
import time
import json
import subprocess
import urllib.request
from uuid import uuid4
from types import MappingProxyType

from .artifacts import ProsaicArtifact, inspect_artifact
from .config import RuntimeConfig
from .effort_evidence import EffortTrace, http_attempt, attach, KEY as EFFORT_KEY
from .effort import api_default_effort
from .events import Cancelled, check_cancelled, event_context, emit
from .tool_event_contract import tool_contract_entry
from .tool_evidence import EvidenceUseDenied
from .tool_result_contract import ToolResultValidationError
from .openai_compatible import OpenAICompatibleBackend
from .anthropic import AnthropicBackend
from .admission import validate_execution_artifact
from .policy import RunPolicy, requested_tools, BUILTIN_TOOLS
from .types import Invocation, Result
from .tools import validate_custom_tools, custom_descriptors, ToolDeadlineExceeded, ToolExecutionError
from .cli_tools import load_cli_tools, cli_tool_context
from .tool_registry import BoundedToolRegistry, DisabledBuiltinRegistry, InvalidToolRequest
from .accounting import AccountingError, resolve_context
from .accounting_capture import Capture, InvalidEvidence, strict_json
from .lifecycle import ExecutionLifecycle, tracked
from .http_bounds import LimitExceeded, set_response_timeout, BoundedNativeStream
from .operation_context import InvocationScope
from .tool_effects import admit_tool_effects, ToolEffectFailure
from .telemetry import ObserverEmitter, observation_context, observe, execution_started
from .budgets import InvocationBudget, InvocationBudgetExceeded
from .turn_capture import TurnOutputExceeded
from .terminal import TerminalCapture, TerminalCompletionError, admit_terminal_tools
from .execution_budget import active_budgets, check_budgets, reserve_dispatch, record_shared_usage, token_reservations
from .context_execution import ContextExecutionFailure
from .rules import check_required_pins, refuse_unenforced, required_guard_specs, validate_guards


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _TurnContextExceeded(LimitExceeded):
    pass


def _reported_usage(details):
    if not isinstance(details, dict) or any(type(v) is not int or v < 0 for v in details.values()):
        return None
    return details.get('total_tokens')


class _BoundedMixin:
    def __init__(self, endpoint, max_input_bytes, custom_tools, deadline, capture=None, budget=None,
                 scope=None, tool_journal=None, cancelled=None, terminal_capture=None, request_headers=(),
                 effort_trace=None, effort_profile=None, requested_effort=None, configured_effort=None):
        super().__init__(endpoint)
        self.effort_trace = effort_trace if effort_trace is not None else EffortTrace()
        self.effort_profile = effort_profile
        self.requested_effort, self.configured_effort = requested_effort, configured_effort
        self.max_input_bytes = max_input_bytes
        self.request_headers = request_headers
        self.reported_token_usage = None
        self.usage_complete = True
        self.turns = 0
        self.custom_tools = custom_tools
        self.execution_budgets = active_budgets()
        self.deadline = min([deadline, *(b.deadline for b in self.execution_budgets)])
        self.capture = capture
        self.budget = budget
        self.scope, self.tool_journal = scope, tool_journal
        self.cancelled = cancelled if cancelled is not None else lambda: False
        self.tool_call_index = 0
        self.reported_usage_details = {}
        self._turn_usage_unknown = False
        self._latest_usage_complete = None
        self._previous_usage_details = {}
        self._provider_attempts = 0
        self._prework_capacity_failure = None
        self.accounting_context = None
        self.turn_mode = False
        self.turn_capture = None
        self.parallel_tools = budget.policy.parallel_tools if budget is not None else None
        self.terminal_capture = terminal_capture
        self.known_usage_details = {}
        self._dispatch_reservation = None
        self._request_token_reservations = {}
        self._captured_usage = None

    def check_boundary(self):
        check_budgets(self.execution_budgets)
        check_cancelled()
        from .conformance import _check_suite_deadline
        _check_suite_deadline()
        if time.monotonic() >= self.deadline:
            raise ToolDeadlineExceeded('invocation deadline exceeded')

    def make_registry(self, cwd, features, metadata):
        builtin = DisabledBuiltinRegistry() if cwd is None else super().make_registry(cwd, features, metadata)
        from .mcp import is_mcp_tool
        allowed_tools = metadata.get('allowed_tools', ())
        index_all_calls = (self.turn_mode or self.parallel_tools is not None
                           or any(is_mcp_tool(self.custom_tools.get(name)) for name in allowed_tools))
        return BoundedToolRegistry(builtin,
                                   self.custom_tools, allowed_tools, self.check_boundary,
                                   self.before_tool_dispatch,
                                   self.tool_execution_options, index_all_calls=index_all_calls,
                                   bounded_recovery=self.budget is not None and self.budget.policy.bounded_tool_recovery,
                                   terminal_capture=self.terminal_capture,
                                   terminal_tools=self.budget.policy.allowed_terminal_tools if self.budget is not None else ())

    def before_tool_dispatch(self):
        if self.budget is not None:
            self.budget._check_reported_allowance()
            cap=self.budget.policy.max_tool_calls
            if cap is not None and self.budget.tool_calls+self.budget._pending_tool_calls>=cap:
                raise InvocationBudgetExceeded('tool_call_limit')
        reserve_dispatch(self.execution_budgets,tool=True)
        if self.budget is not None: self.budget.before_tool()

    def tool_execution_options(self):
        index = self.tool_call_index
        self.tool_call_index += 1
        options = dict(scope=self.scope, deadline=self.deadline, cancelled=self.cancelled,
                       call_index=index, tool_journal=self.tool_journal, accounting_context=self.accounting_context)
        if self.turn_capture is not None:
            options['on_completion'] = lambda outcome, status='completed', **refs: self.turn_capture.record_tool_completion(
                index, outcome, status, **refs)
        return options

    def usage_details(self, parsed):
        details = super().usage_details(parsed)
        # Compatibility parsers omit invalid quantities. A strict cap must see
        # those fields before omission could turn them into trusted evidence.
        if (self.turn_mode or any(b._token_cap is not None for b in self.execution_budgets)
                or (self.budget is not None and self.budget.policy.max_reported_tokens is not None)):
            usage = parsed.get('usage') if isinstance(parsed, dict) else None
            if usage is not None:
                self._latest_usage_complete = _reported_usage(details) is not None
                if any(details[key] != self._previous_usage_details[key]
                       for key in details.keys() & self._previous_usage_details.keys()):
                    self._turn_usage_unknown = True
                self._previous_usage_details.update(details)
                fields = ('prompt_tokens', 'completion_tokens', 'total_tokens')
                if (not isinstance(usage, dict) or any(type(usage[k]) is not int or usage[k] < 0
                        for k in fields if k in usage)):
                    self._turn_usage_unknown = True
                elif all(k in usage for k in fields) and usage['total_tokens'] != usage['prompt_tokens'] + usage['completion_tokens']:
                    self._turn_usage_unknown = True
        return details

    def parse_response_json(self, raw):
        if self.turn_mode:
            try:
                from .conversation import encode_owned
                value = strict_json(raw)
                encode_owned(value, max_bytes=self._config.max_response_bytes,
                             max_depth=self.turn_capture.limits.max_json_depth,
                             max_nodes=self.turn_capture.limits.max_json_nodes)
                return value
            except (ValueError, TypeError, UnicodeError, RecursionError):
                raise InvalidToolRequest() from None
        if (any(b._token_cap is not None for b in self.execution_budgets)
                or (self.budget is not None and self.budget.policy.max_reported_tokens is not None)):
            try:
                return strict_json(raw)
            except InvalidEvidence:
                # Retain ordinary parsed text/details for diagnostics, but never
                # admit dispatch from duplicate/nonfinite evidence Capture rejects.
                self._turn_usage_unknown = True
        return super().parse_response_json(raw)

    def record_turn_usage(self, details):
        from .openai_compatible import _merge_token_usage_details
        self.turns += 1
        self.reported_usage_details = _merge_token_usage_details(self.reported_usage_details, details or {})
        usage = None if self._turn_usage_unknown or self._latest_usage_complete is False else _reported_usage(details)
        if usage is None:
            self.usage_complete = False
        else:
            self.reported_token_usage = (self.reported_token_usage or 0) + usage
            self.known_usage_details = _merge_token_usage_details(self.known_usage_details, details or {})
        local_error=None
        if self.budget is not None:
            try: self.budget.record_usage(usage)
            except InvocationBudgetExceeded as exc: local_error=exc
        record_shared_usage(self.execution_budgets,usage,reservation=self._dispatch_reservation)
        if local_error is not None: raise local_error

    def tool_call_summary(self, tool_call):
        function = tool_call.get('function')
        if isinstance(function, dict) and function.get('name') in self.custom_tools:
            return 'host-registered tool (arguments omitted)'
        return super().tool_call_summary(tool_call)

    def tool_event_metadata(self, name):
        return {'tool_version': self.custom_tools[name].version} if name in self.custom_tools else {}

    def strict_tool_names(self):
        return frozenset(self.custom_tools)

    def preserve_malformed_tool_calls(self):
        return self.budget is not None and self.budget.enabled

    def strict_conversation_calls(self):
        return self.turn_mode

    def check_token_admission(self):
        amounts=self._request_token_reservations
        if amounts and self.budget is not None and self.budget.policy.max_reported_tokens is not None:
            if self.budget.reported_tokens+max(amounts.values())>self.budget.policy.max_reported_tokens:
                raise InvocationBudgetExceeded('token_limit')

    def before_http_dispatch(self, timeout):
        self.check_boundary()
        timeout = min(timeout, self.deadline - time.monotonic())
        if self.budget is not None: self.budget._check_provider()
        amounts=self._request_token_reservations
        self.check_token_admission()
        self._dispatch_reservation=reserve_dispatch(self.execution_budgets,token_reservations=amounts)
        if self.budget is not None:
            self.budget.before_provider()
        self._provider_attempts += 1
        return timeout

    @contextmanager
    def open_http(self, request, *, timeout):
        self._prework_capacity_failure = None
        for name, value in self.request_headers:
            request.add_header(name, value)
        self.check_boundary()
        if any(b._token_counter is not None for b in self.execution_budgets):
            try: native=strict_json(request.data)
            except Exception: raise InvocationBudgetExceeded('token_admission') from None
            self._request_token_reservations=token_reservations(self.execution_budgets,native,self._config)
        if self.budget is not None:
            self.budget._check_provider()
        self.check_token_admission()
        reserve_dispatch(self.execution_budgets,reserve=False,token_reservations=self._request_token_reservations)
        self._turn_usage_unknown = False
        self._latest_usage_complete = None
        self._previous_usage_details = {}
        self._captured_usage = None
        started = time.monotonic()
        self._stream_timed_out = False
        observe('provider_request_started')
        outcome = 'execution_failure'
        status = None
        if self.capture is None:
            call_id = uuid4().hex
            request.add_header("X-Correlation-ID", call_id)
            request.add_header("X-Client-Request-Id", call_id)
        accepted = False
        opener = urllib.request.build_opener(_NoRedirect())
        from .tool_event_contract import current_registry
        evidence_registry = current_registry()
        ticket = (evidence_registry.prepare_delivery(None, encoded_bytes=request.data)
                  if evidence_registry is not None else None)
        delivery_id = None
        def dispatch(timeout):
            nonlocal delivery_id
            row = http_attempt(request.data, backend=self._config.provider, profile=self.effort_profile,
                call_id=request.get_header('X-client-request-id'), requested=self.requested_effort,
                configured=self.configured_effort)
            if ticket is not None:
                delivery_id = request.get_header('X-client-request-id') or uuid4().hex
                evidence_registry.begin_delivery(ticket, delivery_id)
            timeout = self.before_http_dispatch(timeout)
            self.effort_trace.add(row)
            return timeout
        try:
            # Observers and persisted intent preparation can stop execution.
            # Check both boundaries; reserve the attempt only at real dispatch.
            self.check_boundary()
            response = (self.capture.open(opener, request, timeout,
                            before_dispatch=dispatch) if self.capture is not None
                        else opener.open(request, timeout=dispatch(timeout)))
            accepted = True
            with response:
                status = response.status
                if ticket is not None:
                    evidence_registry.confirm_delivery(ticket, delivery_id)
                yield response
            outcome = 'timed_out' if self._stream_timed_out else 'completed'
        except Cancelled:
            outcome = 'cancelled'
            raise
        except (ToolDeadlineExceeded, TimeoutError):
            outcome = 'timed_out'
            raise
        except LimitExceeded:
            outcome = 'budget_failure'
            raise
        except urllib.error.HTTPError as exc:
            status = exc.code
            if (not accepted and self.capture is None and exc.code == 429 and self._provider_attempts == 1
                    and self.budget is not None and self.budget.tool_calls == 0):
                self._prework_capacity_failure = 'rate_limit'
            raise
        except urllib.error.URLError as exc:
            if (not accepted and self.capture is None and isinstance(exc.reason, ConnectionRefusedError) and self._provider_attempts == 1
                    and self.budget is not None and self.budget.tool_calls == 0):
                self._prework_capacity_failure = 'unavailable'
            raise
        finally:
            observe('provider_request_completed', outcome=outcome, http_status=status,
                    duration_ms=(time.monotonic() - started) * 1000)

    def read_response(self, response):
        self.check_boundary()
        limit = self._config.max_response_bytes
        if callable(getattr(response, 'read1', None)):
            chunks, size = [], 0
            while True:
                self.check_boundary()
                set_response_timeout(response, self.deadline - time.monotonic())
                chunk = response.read1(min(65536, limit - size + 1))
                size += len(chunk)
                if size > limit:
                    raise LimitExceeded('response exceeds max_response_bytes')
                if not chunk:
                    break
                chunks.append(chunk)
            body = b''.join(chunks)
        else:
            body = response.read(limit + 1)
        self.check_boundary()
        if len(body) > limit:
            raise LimitExceeded("response exceeds max_response_bytes")
        return body.decode("utf-8", errors="strict")

    def _read_sse_turn(self, response, deadline):
        if not isinstance(response, BoundedNativeStream):
            response = BoundedNativeStream(response, self._config.max_response_bytes, min(deadline, self.deadline))
        turn = super()._read_sse_turn(response, deadline)
        # Readers may convert transport/deadline timeouts into Result instead
        # of raising. Carry that state to the still-active HTTP observation.
        if isinstance(turn, Result) and turn.timed_out:
            self._stream_timed_out = True
        return turn

    def validate_payload(self, payload):
        check_cancelled()
        if self.turn_mode:
            # Turn requests are guarded once, on their actual native bytes.
            return
        if len(json.dumps(payload).encode()) > self.max_input_bytes:
            raise LimitExceeded("conversation exceeds max_input_bytes")

    def encode_payload(self, payload):
        guard = getattr(self, 'context_guard', None)
        encoder = super().encode_payload
        data = guard.construct_native(lambda: encoder(payload)) if guard is not None else encoder(payload)
        if guard is not None:
            guard.check_wire(payload, deadline=self.deadline, encoded_bytes=len(data))
        if self.turn_mode and len(data) > self.max_input_bytes:
            raise _TurnContextExceeded('contextLimit')
        return data

    def native_payload(self, payload):
        converter = super().native_payload
        guard = getattr(self, 'context_guard', None)
        return guard.construct_native(lambda: converter(payload)) if guard is not None else converter(payload)

    def _post_chat_turn(self, payload, request, deadline, streaming):
        check_cancelled()
        if time.monotonic() >= deadline:
            return Result(1, "", "invocation deadline exceeded", timed_out=True)
        self.validate_payload(payload)
        attempted_before = self._provider_attempts
        turn = super()._post_chat_turn(payload, request, deadline, streaming)
        if self.turn_mode and isinstance(turn, Result) and turn.metadata.get('provider_error_code') in {'malformed_response', 'malformed_sse'}:
            turn.metadata['failure_reason'] = 'invalidToolRequest'
        attempted = self._provider_attempts > attempted_before
        native_failure = (isinstance(turn, Result) and self._config.provider == 'anthropic' and
            (attempted or _reported_usage(turn.metadata.get('token_usage_details')) is not None))
        strict_attempt_failure = (isinstance(turn, Result) and self.budget is not None and
            (self.turn_mode or self.execution_budgets or self.budget.policy.max_reported_tokens is not None) and
            attempted)
        if not isinstance(turn, Result) or native_failure or strict_attempt_failure:
            details = turn.metadata.get('token_usage_details') if isinstance(turn, Result) else turn.token_usage_details
            try:
                self.record_turn_usage(details)
            except InvocationBudgetExceeded as exc:
                if not isinstance(turn, Result):
                    turn = Result(1, turn.text, str(exc), metadata={
                        'provider': self.name, 'finish_reason': turn.finish_reason,
                        'streamed': turn.streamed, 'http_status': turn.http_status,
                        'raw_response_headers': turn.raw_response_headers,
                        'raw_response_metadata': turn.raw_response_metadata,
                        'token_usage_details': self.reported_usage_details})
                turn.exit_code = 1
                turn.stderr = str(exc)
                turn.metadata['failure_reason'] = exc.reason
        return turn


class _BoundedBackend(_BoundedMixin, OpenAICompatibleBackend):
    pass


class _BoundedAnthropicBackend(_BoundedMixin, AnthropicBackend):
    pass


class ProsaicRuntime:
    capabilities = frozenset({'conformance_v1', 'invocation_budgets_v1', 'observer_v1', 'accounting_v1', 'read_receipts_v1', 'initial_tool_v1', 'initial_tool_enforcement_v1', 'acquisition_v1', 'custom_tools_v1', 'cli_tools_v1', 'cli_sandbox_v1', 'mcp_client_v1'})
    capabilities |= frozenset({'tool_context_v1', 'tool_journal_v1'})
    capabilities |= frozenset({'conversation_turn_v1'})
    capabilities |= frozenset({'context_assembly_v1', 'http_correlation_v1', 'execution_lifecycle_v1'})
    capabilities |= frozenset({'token_admission_v1'})
    capabilities |= frozenset({'tool_outcomes_v1', 'bounded_tool_recovery_v1', 'direct_tool_v1'})
    capabilities |= frozenset({'terminal_tool_v1'})
    capabilities |= frozenset({'rule_admission_v1'})
    capabilities |= frozenset({'accounting_request_metadata_v1', EFFORT_KEY})
    capabilities |= frozenset({'guard_input_views_v1'})
    capabilities |= frozenset({'frozen_turn_inputs_v1'})
    capabilities |= frozenset({'context_input_variants_v1'})
    capabilities |= frozenset({'tool_events_v1', 'tool_results_v1', 'citations_v1', 'parallel_tool_calls_v1'})
    def __init__(self, config: RuntimeConfig, *, source=".prosaic", executable="prosaic", custom_tools=None,
                 accounting=None, context_defaults=None, lifecycle=None, guards=None, allow_unbound_guards=False,
                 rule_retries=1, correct_malformed_tool_calls=False):
        self.lifecycle = lifecycle if lifecycle is not None else ExecutionLifecycle()
        if not isinstance(self.lifecycle, ExecutionLifecycle):
            raise TypeError("lifecycle must be an ExecutionLifecycle")
        self.config = config
        self.source = Path(source)
        self.executable = executable
        self.accounting = accounting
        self.context_defaults = context_defaults
        self._custom_tools = validate_custom_tools(custom_tools)
        self._cli_tools = load_cli_tools(config.tool_directories)
        if self._custom_tools.keys() & self._cli_tools.keys():
            raise ValueError('duplicate custom tool registration and CLI manifest')
        self._custom_tools.update({name: tool.custom_tool() for name, tool in self._cli_tools.items()})
        # Guards are host code registered here, like custom tools. Markdown
        # binds only what is registered; registering nothing changes nothing.
        self._guards = validate_guards(guards)
        check_required_pins(required_guard_specs(config.required_guards), self._guards)
        # Resolved now: a later chdir must not move where path-like rule settings point.
        self._project_root = Path(source).resolve().parent
        if type(allow_unbound_guards) is not bool:
            raise ValueError('allow_unbound_guards must be boolean')
        self._allow_unbound_guards = allow_unbound_guards
        # Host policy for conversation turns, like the guards themselves. Constructor arguments rather than
        # RuntimeConfig fields: a new configuration field would change every Harness workflow identity.
        if type(correct_malformed_tool_calls) is not bool:
            raise ValueError('correct_malformed_tool_calls must be boolean')
        self._correct_malformed_tool_calls = correct_malformed_tool_calls
        from .rule_hooks import MAX_RULE_RETRIES
        if type(rule_retries) is not int or not 0 <= rule_retries <= MAX_RULE_RETRIES:
            raise ValueError(f'rule_retries must be an integer from 0 to {MAX_RULE_RETRIES}')
        self._rule_retries = rule_retries

    @property
    def tool_descriptors(self):
        return MappingProxyType(custom_descriptors(self._custom_tools))

    @property
    def cli_tool_names(self):
        return frozenset(self._cli_tools)

    def rule_coverage(self, artifact):
        """Enforcement coverage against this runtime's registered guards: ids and counts, no inference."""
        from .rules import rule_coverage
        if isinstance(artifact, str):
            artifact = inspect_artifact(artifact, self.source, executable=self.executable)
        if not isinstance(artifact, ProsaicArtifact):
            raise TypeError('rule_coverage requires a Prosaic artifact identifier or inspection artifact')
        return rule_coverage(artifact, self._guards)

    def prepare_turn(self, artifact, *, message, history=(), input_data=None,
                     policy=None, limits=None, workspace=None, context_policy=None,
                     token_counters=None, context_selection=None, context_report=None, context_work=None,
                     _warn_rules=True, input_profile=None, authoritative_input_data=None,
                     authoritative_input_profile=None, authoritative_input_limits=None, tool_contract_bindings=None):
        """Purely admit and own one role-aware conversational input."""
        from .conversation_preparation import prepare_turn
        return prepare_turn(self, artifact, message=message, history=history,
                            input_data=input_data, policy=policy, limits=limits, workspace=workspace,
                            context_policy=context_policy, token_counters=token_counters,
                            context_selection=context_selection, context_report=context_report,
                            context_work=context_work, _warn_rules=_warn_rules,input_profile=input_profile,
                            authoritative_input_data=authoritative_input_data,
                            authoritative_input_profile=authoritative_input_profile,authoritative_input_limits=authoritative_input_limits,
                            tool_contract_bindings=tool_contract_bindings)

    @tracked
    @tool_contract_entry
    def turn(self, artifact, *, message=None, history=(), input_data=None,
             policy=None, limits=None, workspace=None, on_event=None, cancelled=None,
             env=None, context=None, accounting=None, observer=None,
             operation_context=None, tool_journal=None, context_policy=None,
             token_counters=None, context_work=None, context_checkpoint=None, on_admitted=None, request_headers=None,
             input_profile=None, authoritative_input_data=None, authoritative_input_profile=None, authoritative_input_limits=None,
             tool_contract_bindings=None):
        """Execute one bounded role-aware turn; streamed events are provisional."""
        from .request_headers import snapshot_headers
        owned_headers = snapshot_headers(request_headers)
        from .conversation import PreparedTurn, ConversationMessage, TurnLimits, TurnAdmissionError
        from .conversation_preparation import policy_from_document, execution_snapshot
        from .turn_capture import TurnCapture
        guard = None
        if type(artifact) is PreparedTurn:
            if message is not None or history or input_data is not None or policy is not None or limits is not None or workspace is not None or context_policy is not None or any(v is not None for v in (input_profile,authoritative_input_data,authoritative_input_profile,authoritative_input_limits)):
                raise TurnAdmissionError('invalidPolicy')
            try:
                owned = json.loads(artifact._encoded)
                if owned.get('toolContractV1') != (tool_contract_bindings.to_dict() if tool_contract_bindings is not None else None):
                    raise TurnAdmissionError('bindingMismatch')
                approved = ProsaicArtifact.from_inspection(owned['agent'])
                context_args = {}
                if 'contextPolicy' in owned:
                    from .context_contracts import ContextPolicy
                    if context_work is not None:
                        original = artifact.context_work
                        for field in ('candidate_checks', 'candidate_bytes', 'active_assembly_micros',
                                      'reserved_active_micros', 'checkpoint_sequence'):
                            if getattr(context_work, field) < getattr(original, field):
                                raise TurnAdmissionError('bindingMismatch')
                    context_args = dict(context_policy=ContextPolicy.from_dict(owned['contextPolicy']),
                        token_counters=token_counters, context_selection=artifact.context_selection,
                        context_report=artifact.context_report,
                        context_work=context_work if context_work is not None else artifact.context_work)
                elif token_counters is not None or context_work is not None:
                    raise TurnAdmissionError('invalidPolicy')
                if artifact.encoding_version in ('conversation-turn-v3', 'conversation-turn-v4', 'conversation-turn-v5'):
                    from .input_views import AuthoritativeInputLimits
                    context_args.update(input_profile=owned.get('inputProfile'),
                        authoritative_input_data=owned.get('authoritativeInputData'),
                        authoritative_input_profile=owned.get('authoritativeInputProfile'),
                        authoritative_input_limits=AuthoritativeInputLimits(**owned['authoritativeInputLimits']) if owned.get('authoritativeInputLimits') is not None else None)
                # Readmission repeats admission silently; warnings were raised at preparation.
                if artifact.encoding_version in ('conversation-turn-v3', 'conversation-turn-v4', 'conversation-turn-v5'):
                    from .context_contracts import ContextPolicy
                    prepared=self.restore_turn(approved,
                        history=tuple(ConversationMessage.from_dict(m) for m in owned['history']),
                        frozen_input=artifact.export_frozen_input(),policy=policy_from_document(owned['policy']),
                        limits=TurnLimits(**owned['limits']),workspace=owned['workspace'],
                        context_policy=ContextPolicy.from_dict(owned['contextPolicy']) if 'contextPolicy' in owned else None,
                        token_counters=token_counters,context_work=context_work,tool_contract_bindings=tool_contract_bindings)
                else:
                    prepared = self.prepare_turn(approved, message=owned['message'],
                        history=tuple(ConversationMessage.from_dict(m) for m in owned['history']),
                        input_data=owned['inputData'], policy=policy_from_document(owned['policy']),
                        limits=TurnLimits(**owned['limits']), workspace=owned['workspace'], _warn_rules=False,
                        **context_args)
                if (artifact.encoding_version != prepared.encoding_version or
                        artifact.context_identity != prepared.context_identity or
                        artifact.input_bytes != prepared.input_bytes or artifact._encoded != prepared._encoded):
                    raise TurnAdmissionError('bindingMismatch')
            except (ValueError, TypeError, KeyError, AttributeError, UnicodeError) as exc:
                raise TurnAdmissionError('bindingMismatch') from exc
        else:
            prepared = self.prepare_turn(artifact, message=message, history=history,
                input_data=input_data, policy=policy, limits=limits, workspace=workspace,
                context_policy=context_policy, token_counters=token_counters,input_profile=input_profile,
                authoritative_input_data=authoritative_input_data,authoritative_input_profile=authoritative_input_profile,
                authoritative_input_limits=authoritative_input_limits,tool_contract_bindings=tool_contract_bindings)
            if context_work is not None:
                raise TurnAdmissionError('invalidPolicy')
            owned = json.loads(prepared._encoded)
            approved = ProsaicArtifact.from_inspection(owned['agent'])
        policy = policy_from_document(owned['policy'])
        limits = TurnLimits(**owned['limits'])
        # Compiled once at (re)admission and carried privately on the prepared turn.
        ruleset = prepared._rules
        owned['_ruleSet'] = ruleset

        def with_rules(turn_result):
            correction = owned.get('_toolRequestCorrection')
            if correction is not None and correction.issues:
                turn_result = replace(turn_result, tool_request_corrections=correction.issues)
            hooks = owned.get('_ruleHooks')
            if hooks is not None:
                return replace(turn_result, **hooks.report())
            if ruleset.bindings:
                return replace(turn_result, rules_fingerprint=ruleset.rules_fingerprint())
            return turn_result
        snapshot = execution_snapshot(self, owned)
        if owned_headers:
            from .conversation_preparation import turn_endpoint
            if turn_endpoint(self, approved, policy)[1].provider != 'openai-compatible':
                raise ValueError('request_headers require an OpenAI-compatible endpoint')
        scope = InvocationScope(str(uuid4())) if operation_context is None else operation_context
        # Static effect admission precedes callbacks/observation as well as I/O.
        try:
            from .mcp import admit_mcp_tools
            requested = requested_tools(approved.frontmatter.get('tools'))
            admit_mcp_tools(self._custom_tools, requested, surface="api", policy=policy,
                            scope=scope, journal=tool_journal, contracts=tool_contract_bindings,
                            initial_tool=policy.initial_tool, execution=True)
            admit_tool_effects(self._custom_tools, requested, scope, tool_journal)
        except (ValueError, TypeError, AttributeError) as exc:
            raise TurnAdmissionError('invalidPolicy') from exc
        capture, budget = TurnCapture(owned['message'], scope, limits), InvocationBudget(policy)
        if 'inputViews' in owned:
            capture.bind_input_views(owned['inputViews'])
        trace = EffortTrace()
        capture.effort_trace = trace
        # Freeze attribution and the destination before any host observation.
        recorder = accounting if accounting is not None else self.accounting
        enabled = recorder is not None or context is not None or self.context_defaults is not None
        try:
            resolved = resolve_context(context, getattr(recorder, 'defaults', None) or self.context_defaults) if enabled else None
            if resolved is not None:
                from .accounting import ResolvedContext
                resolved = ResolvedContext.from_dict(resolved.to_dict())
        except (ValueError, TypeError, AttributeError):
            raise TurnAdmissionError('invalidPolicy') from None
        frozen_accounting = (recorder, resolved, enabled)
        if 'contextPolicy' in owned:
            if enabled:
                # Freeze evidence attribution/headroom before readmission or the
                # host marker. This creates no accounting intent or model effect.
                capture.bind_accounting(resolved, recorder, policy.max_provider_requests, policy.max_tool_calls)
            from .context_contracts import ContextPolicy
            from .context_counters import admit_counter
            from .context_execution import ContextExecutionGuard
            from .conversation_preparation import initial_payload, turn_endpoint
            context_policy = ContextPolicy.from_dict(owned['contextPolicy'])
            _, endpoint, _ = turn_endpoint(self, approved, policy)
            registration = admit_counter(context_policy, token_counters, endpoint)
            guard = ContextExecutionGuard(context_policy, registration, prepared.context_report,
                prepared.context_work, input_byte_cap=policy.max_input_bytes,
                checkpoint=context_checkpoint, deadline=time.monotonic() + policy.timeout_s,
                expected_native_digest=owned.get('nativePayloadDigest'))
            try:
                guard.readmit(lambda: initial_payload(self, approved, policy, owned['selectedHistory'],
                    owned['message'], owned['inputData'], context_policy=context_policy,
                    canonical_messages=prepared.encoding_version in ('conversation-turn-v3', 'conversation-turn-v4', 'conversation-turn-v5'))[-1])
            except ToolDeadlineExceeded:
                terminal = capture.finish(Result(1, '', 'timeout', timed_out=True,
                    metadata={'failure_reason': 'invocation_timeout'}), budget)
                return with_rules(replace(terminal, context_report=guard.report, context_work=guard.work))
            owned['_contextGuard'] = guard
        elif prepared.encoding_version in ('conversation-turn-v3', 'conversation-turn-v4', 'conversation-turn-v5'):
            from .conversation_preparation import initial_payload,native_payload_digest
            native=initial_payload(self,approved,policy,owned['history'],owned['message'],owned['inputData'],canonical_messages=True)[-1]
            if native_payload_digest(native)!=owned['nativePayloadDigest'] or len(json.dumps(native,allow_nan=False).encode('utf-8'))!=prepared.input_bytes:
                raise TurnAdmissionError('bindingMismatch')
        if guard is not None and cancelled is not None and cancelled():
            terminal = capture.finish(Result(1, '', 'cancelled',
                metadata={'failure_reason': 'cancelled'}), budget)
            return with_rules(replace(terminal, context_report=guard.report, context_work=guard.work))
        if on_admitted is not None:
            on_admitted()
        emitter = ObserverEmitter(observer, source='runtime', scope=scope)
        started, result = time.monotonic(), None
        with observation_context(emitter, self._custom_tools):
            try:
                emitter.emit('invocation_started')
                result = self._run_accounted(approved, cwd=owned['workspace'], policy=policy,
                    on_event=lambda event: capture.event(event, on_event), cancelled=cancelled, env=env, context=context, accounting=accounting,
                    operation_context=scope, tool_journal=tool_journal,
                    _turn_prepared=owned, _turn_capture=capture, _budget=budget, _turn_snapshot=snapshot,
                    _turn_accounting=frozen_accounting, _request_headers=owned_headers, _effort_trace=trace)
            except TurnAdmissionError:
                raise
            except Exception:
                # Callback failures can happen after a provider/tool effect.
                # They must still return bounded evidence to the session host.
                result = Result(1, '', 'critical_hook_error', metadata={'failure_reason': 'critical_hook_error'})
            finally:
                emitter.emit('invocation_completed', outcome='completed' if result is not None and result.exit_code == 0 else 'execution_failure',
                    duration_ms=(time.monotonic() - started) * 1000,
                    exit_code=result.exit_code if result is not None else None)
        completed = capture.finish(result, budget)
        completed = replace(completed, context_report=guard.report, context_work=guard.work) if guard is not None else completed
        return with_rules(completed)

    @classmethod
    def from_config(cls, path=None, **kwargs):
        return cls(RuntimeConfig.load(path), **kwargs)

    def restore_turn(self,artifact,*,history=(),frozen_input,policy,limits,context_policy=None,
                     token_counters=None,workspace=None,context_work=None,fresh_attempt=False,tool_contract_bindings=None):
        from .frozen_turns import restore_turn
        return restore_turn(self,artifact,history=history,frozen_input=frozen_input,policy=policy,limits=limits,
            context_policy=context_policy,token_counters=token_counters,workspace=workspace,
            context_work=context_work,fresh_attempt=fresh_attempt,tool_contract_bindings=tool_contract_bindings)

    def prepare_turn_variants(self,artifact,*,message,history=(),input_data_variants,policy=None,
                              limits=None,workspace=None,context_policy,token_counters,input_profile=None,
                              authoritative_input_data=None,authoritative_input_profile=None,authoritative_input_limits=None,tool_contract_bindings=None):
        from .input_variants import prepare_turn_variants
        return prepare_turn_variants(self,artifact,message=message,history=history,input_data_variants=input_data_variants,
            policy=policy,limits=limits,workspace=workspace,context_policy=context_policy,token_counters=token_counters,
            input_profile=input_profile,authoritative_input_data=authoritative_input_data,
            authoritative_input_profile=authoritative_input_profile,authoritative_input_limits=authoritative_input_limits,
            tool_contract_bindings=tool_contract_bindings)

    def _preflight_names(self, names, cwd, policy, env, deadline, *, _snapshot=None):
        config, custom, cli = _snapshot if _snapshot is not None else (self.config, self._custom_tools, self._cli_tools)
        checks = {}
        for name in sorted(names):
            error = None
            if name not in BUILTIN_TOOLS and name not in custom:
                error = 'not_registered'
            elif name not in config.allowed_tools or name not in policy.allowed_tools:
                error = 'not_granted'
            elif name in cli:
                try:
                    cli[name].preflight(cwd, policy, env, deadline, config.cli_sandbox)
                except ToolExecutionError as exc:
                    error = exc.code
            else:
                from .policy import READ_TOOLS, WRITE_TOOLS
                if name in READ_TOOLS and not policy.read_roots:
                    error = 'missing_read_roots'
                elif name in WRITE_TOOLS and not policy.write_paths:
                    error = 'missing_write_paths'
            checks[name] = {'status': 'error' if error else 'ok',
                            'message': error or ('registration_available' if name not in cli else 'cli_available')}
        return {'ok': all(c['status'] == 'ok' for c in checks.values()), 'checks': checks,
                'inference': False}

    def preflight(self, artifact=None, *, all_tools=False, cwd='.', policy=None, env=None):
        """Offline tool availability/authority check; trusted probes may execute, never inference."""
        policy = policy or RunPolicy(timeout_s=self.config.limits.timeout_s,
                                     max_tool_rounds=self.config.limits.max_tool_rounds)
        deadline = time.monotonic() + policy.timeout_s
        if all_tools and artifact is not None:
            raise ValueError('choose an artifact or all_tools, not both')
        if not all_tools and artifact is None:
            raise ValueError('preflight requires an artifact or all_tools')
        if isinstance(artifact, str):
            artifact = inspect_artifact(artifact, self.source, executable=self.executable,
                                        timeout_s=min(30, policy.timeout_s))
        if artifact is not None and not isinstance(artifact, ProsaicArtifact):
            raise TypeError('preflight requires a Prosaic artifact')
        names = (self._custom_tools.keys() | self.config.allowed_tools) if all_tools else requested_tools(artifact.frontmatter.get('tools'))
        return self._preflight_names(names, str(Path(cwd).resolve()), policy,
                                     dict(os.environ if env is None else env), deadline)

    @tracked
    @tool_contract_entry
    def run_tool(self, name, arguments, *, policy=None, cwd='.', on_event=None,
                 cancelled=None, operation_context=None, tool_journal=None,
                 observer=None, context=None, accounting=None, tool_contract_bindings=None) -> Result:
        """Execute one granted host CustomTool with literal JSON arguments and no model.

        Builtin and CLI tools are unsupported. Trusted callbacks cooperate with
        cancellation/deadlines; unknown effects require host reconciliation.
        """
        from .direct_tool import run_tool
        return run_tool(self, name, arguments, policy=policy, cwd=cwd,
                        on_event=on_event, cancelled=cancelled, operation_context=operation_context,
                        tool_journal=tool_journal, observer=observer, context=context, accounting=accounting)

    @tracked
    @tool_contract_entry
    def run(self, artifact: str | ProsaicArtifact, arguments: str = "", *,
            cwd: str | Path = '.', policy: RunPolicy | None = None,
            on_event=None, cancelled=None, env=None,
            acquisition: str | ProsaicArtifact | None = None, context=None, accounting=None,
            observer=None, operation_context: InvocationScope | None = None, tool_journal=None, request_headers=None,
            tool_contract_bindings=None) -> Result:
        from .request_headers import snapshot_headers
        owned_headers = snapshot_headers(request_headers)
        scope = InvocationScope(str(uuid4())) if operation_context is None else operation_context
        emitter = ObserverEmitter(observer, source='runtime', scope=scope)
        started = time.monotonic()
        result = None
        outcome = 'execution_failure'
        with observation_context(emitter, self._custom_tools):
            try:
                emitter.emit('invocation_started')
                result = self._run_accounted(artifact, arguments, cwd=cwd, policy=policy,
                    on_event=on_event, cancelled=cancelled, env=env, acquisition=acquisition,
                    context=context, accounting=accounting, operation_context=scope, tool_journal=tool_journal,
                    _request_headers=owned_headers)
                if result.metadata.get('failure_reason') == 'cancelled':
                    outcome = 'cancelled'
                elif result.timed_out:
                    outcome = 'timed_out'
                elif result.metadata.get('failure_reason') in InvocationBudgetExceeded.REASONS | {'budget_exceeded'}:
                    outcome = 'budget_failure'
                else:
                    outcome = 'completed' if result.exit_code == 0 else 'execution_failure'
                return result
            except Exception:
                outcome = ('critical_hook_error' if emitter.critical_hook_failed else
                           'execution_failure' if emitter.execution_started else 'admission_failure')
                raise
            finally:
                if emitter.critical_hook_failed:
                    outcome = 'critical_hook_error'
                emitter.emit('invocation_completed', outcome=outcome,
                    duration_ms=(time.monotonic() - started) * 1000,
                    exit_code=result.exit_code if result is not None else None,
                    token_usage=result.token_usage if result is not None else None,
                    reason=result.metadata.get('failure_reason') if result is not None else None)

    def _run_accounted(self, artifact: str | ProsaicArtifact, arguments: str = "", *,
            cwd: str | Path = '.', policy: RunPolicy | None = None,
            on_event=None, cancelled=None, env=None,
            acquisition: str | ProsaicArtifact | None = None, context=None, accounting=None,
            operation_context=None, tool_journal=None, _turn_prepared=None, _turn_capture=None, _budget=None,
            _turn_snapshot=None, _turn_accounting=None, _request_headers=(), _effort_trace=None) -> Result:
        policy = policy or RunPolicy(timeout_s=self.config.limits.timeout_s,
                                     max_tool_rounds=self.config.limits.max_tool_rounds)
        budget = _budget if _budget is not None else InvocationBudget(policy)
        if _turn_accounting is None:
            recorder = accounting if accounting is not None else self.accounting
            enabled = recorder is not None or context is not None or self.context_defaults is not None
            resolved = resolve_context(context, getattr(recorder, 'defaults', None) or self.context_defaults) if enabled else None
        else:
            recorder, resolved, enabled = _turn_accounting
        if _turn_capture is not None and enabled:
            _turn_capture.bind_accounting(resolved, recorder, policy.max_provider_requests,
                                          policy.max_tool_calls if requested_tools(artifact.frontmatter.get('tools')) else 0)
        trace = _effort_trace if _effort_trace is not None else EffortTrace()
        captures = []
        terminal = TerminalCapture()
        try:
            result = self._run(artifact, arguments, _recorder=recorder, _context=resolved,
                               _capture_ref=captures.append, _budget=budget, cwd=cwd, policy=policy, on_event=on_event,
                               cancelled=cancelled, env=env, acquisition=acquisition,
                               _operation_context=operation_context, _tool_journal=tool_journal,
                               _turn_prepared=_turn_prepared, _turn_capture=_turn_capture, _turn_snapshot=_turn_snapshot,
                               _terminal_capture=terminal, _request_headers=_request_headers, _effort_trace=trace)
        except AccountingError:
            result = Result(1, '', 'accounting persistence failed; do not retry the provider automatically',
                            token_usage=budget.reported_tokens if budget.usage_complete and budget.provider_requests else None,
                            metadata={'failure_reason': 'accounting_failed','reported_token_usage':budget.reported_tokens})
        except TerminalCompletionError as exc:
            result = Result(1, '', exc.reason,
                token_usage=budget.reported_tokens if budget.usage_complete else None,
                metadata={'failure_reason': exc.reason})
        except Exception:
            if terminal.evidence is None:
                raise
            result = Result(1, '', 'critical_hook_error',
                token_usage=budget.reported_tokens if budget.usage_complete else None,
                metadata={'failure_reason': 'critical_hook_error'})
        if terminal.evidence is not None:
            result.metadata['terminalCompletion'] = terminal.evidence
        if enabled:
            # Capture lineage is supplied by the result when a request began;
            # failures before capture still expose the resolved attribution.
            result.metadata.setdefault('accounting_v1', {'context': resolved.to_dict(),
                'namespace': getattr(recorder, 'namespace', None), 'environment': getattr(recorder, 'environment', None),
                'provider_call_ids': list(captures[0].call_ids) if captures else []})
        strict=[b for b in active_budgets() if b.token_admission is not None]
        if strict:
            result.metadata['token_admission_v1']=[b.token_admission for b in strict]
            if any(b.reservation_exceeded for b in active_budgets()):
                result.metadata['token_admission_violation']='token_reservation_exceeded'
            if not budget.usage_complete:
                result.token_usage=None
                result.metadata['token_usage_status']='unknown'
        if budget.enabled or strict or policy.allowed_terminal_tools:
            result.metadata['invocation_budgets_v1'] = budget.metadata()
        if policy.parallel_tools is not None:
            from .conversation import encode_owned
            result.metadata['parallel_tools_v1']=json.loads(encode_owned(dict(version=1,scheduler='independent-read-windows-v1',
                policy=policy.parallel_tools.to_dict(),batches=budget.parallel_batches),max_bytes=65536))
        return attach(result, trace)

    def _run(self, artifact: str | ProsaicArtifact, arguments: str = "", *,
            cwd: str | Path = ".", policy: RunPolicy | None = None,
            on_event=None, cancelled=None, env=None,
            acquisition: str | ProsaicArtifact | None = None, _recorder=None, _context=None, _capture_ref=None,
            _budget=None, _operation_context=None, _tool_journal=None,
            _turn_prepared=None, _turn_capture=None, _turn_snapshot=None, _terminal_capture=None, _request_headers=(), _effort_trace=None) -> Result:
        config, custom, cli = _turn_snapshot if _turn_snapshot is not None else (self.config, self._custom_tools, self._cli_tools)
        policy = policy or RunPolicy(timeout_s=self.config.limits.timeout_s,
                                     max_tool_rounds=self.config.limits.max_tool_rounds)
        started = time.monotonic()
        backend = None
        invocation_env = dict(os.environ if env is None else env)
        cli_context = (nullcontext() if cwd is None else cli_tool_context(
            cwd, policy, invocation_env, started + policy.timeout_s, config.cli_sandbox))
        with event_context(on_event, cancelled), cli_context:
            try:
                check_cancelled()
                if isinstance(artifact, str):
                    artifact = inspect_artifact(artifact, self.source, executable=self.executable,
                                                timeout_s=min(30, policy.timeout_s))
                if not isinstance(artifact, ProsaicArtifact):
                    raise TypeError("artifact must be a Prosaic artifact identifier or inspection artifact")
                artifact = ProsaicArtifact.from_inspection(artifact.inspection())
                if _turn_prepared is None:
                    # No rule hooks here: neither an enforce tag nor a host-required
                    # guard may look enforced but run unchecked.
                    if config.required_guards:
                        raise ValueError('required_guards need conversation turn admission; run() cannot enforce them')
                    refuse_unenforced(artifact)
                validate_execution_artifact(artifact, config)
                requested = requested_tools(artifact.frontmatter.get("tools"))
                from .tool_contracts import admit_tool_contracts
                from .tool_event_contract import current_bindings
                from .mcp import admit_mcp_tools
                admit_mcp_tools(custom, requested, surface="api", policy=policy,
                                scope=_operation_context, journal=_tool_journal,
                                contracts=current_bindings(), initial_tool=policy.initial_tool,
                                execution=True)
                admit_tool_contracts(custom, requested, current_bindings())
                from .tool_parallel_contracts import admit_parallel_execution
                admit_parallel_execution(policy,current_bindings(),metadata_limit=_turn_capture.limits.max_metadata_bytes if _turn_capture is not None else 65536)
                from .tool_event_contract import current_registry
                if policy.parallel_tools is not None:
                    current_registry().parallel_order=True
                if current_bindings() is not None and (policy.max_provider_requests is None or policy.max_tool_calls is None):
                    raise ValueError('tool contracts require finite attempt caps')
                admit_tool_effects(custom, requested, _operation_context, _tool_journal)
                unsupported = requested - BUILTIN_TOOLS - custom.keys()
                if unsupported:
                    raise ValueError(f"unsupported tools: {sorted(unsupported)}")
                cli_requested = requested & cli.keys()
                if cli_requested:
                    report = self._preflight_names(cli_requested, str(Path(cwd).resolve()), policy,
                                                   invocation_env, started + policy.timeout_s, _snapshot=_turn_snapshot)
                    if not report['ok']:
                        failed = {name: check['message'] for name, check in report['checks'].items() if check['status'] != 'ok'}
                        raise ValueError(f'CLI tool preflight failed: {failed}')
                tools = requested & config.allowed_tools & frozenset(policy.allowed_tools)
                terminal_tools = admit_terminal_tools(custom, tools, policy.allowed_terminal_tools,
                                                       supported=_turn_prepared is None)
                if policy.initial_tool is not None and policy.initial_tool not in tools:
                    raise ValueError('initial_tool must be granted by prose, runtime and host')
                if acquisition is not None:
                    if isinstance(acquisition, str):
                        remaining = policy.timeout_s - (time.monotonic() - started)
                        if remaining <= 0:
                            return Result(1, '', 'invocation deadline exceeded', timed_out=True)
                        acquisition = inspect_artifact(acquisition, self.source, executable=self.executable,
                                                       timeout_s=min(30, remaining))
                    if not isinstance(acquisition, ProsaicArtifact):
                        raise TypeError('acquisition must be a Prosaic artifact identifier or inspection artifact')
                    acquisition = ProsaicArtifact.from_inspection(acquisition.inspection())
                    refuse_unenforced(acquisition)
                    validate_execution_artifact(artifact, config, acquisition=acquisition)
                    acquisition_tools = requested_tools(acquisition.frontmatter.get('tools'))
                    admit_mcp_tools(custom, acquisition_tools, surface="acquisition")
                    if acquisition_tools - requested or policy.initial_tool not in acquisition_tools:
                        raise ValueError('acquisition requires an explicit initial_tool and tools granted by final prose, runtime and host')
                    acquisition_tools &= tools
                tier = artifact.frontmatter.get("model_tier")
                profile = config.routes[tier] if tier is not None else config.default_profile
                endpoint = config.profiles[profile]
                if _request_headers and endpoint.provider != 'openai-compatible':
                    raise ValueError('request_headers require an OpenAI-compatible endpoint')
                if _recorder is not None and _recorder.provider_id != endpoint.provider:
                    raise ValueError('accounting provider_id must match endpoint provider')
                features = {**endpoint.features, "tool_calls": bool(tools), "max_tool_rounds": policy.max_tool_rounds,
                            "web_tools": False, "transcript": False}
                endpoint = replace(endpoint, features=features)
                metadata = {"model": endpoint.model, "allowed_tools": sorted(tools),
                            "tool_read_roots": list(policy.read_roots),
                            "tool_write_paths": list(policy.write_paths),
                            "tool_forbidden_roots": list(policy.forbidden_roots)}
                if policy.initial_tool is not None:
                    metadata['initial_tool'] = policy.initial_tool
                effort = artifact.frontmatter.get("effort")
                if effort is not None:
                    metadata["effort"] = effort
                prompt = artifact.render(arguments)
                if len(prompt.encode()) > policy.max_input_bytes:
                    raise ValueError("artifact and arguments exceed max_input_bytes")
                if acquisition is not None:
                    metadata['final_prompt'] = prompt
                    metadata['acquisition_tools'] = sorted(acquisition_tools)
                    prompt = acquisition.render()
                    if len(prompt.encode()) > policy.max_input_bytes:
                        raise ValueError('acquisition exceeds max_input_bytes')
                remaining = policy.timeout_s - (time.monotonic() - started)
                if remaining <= 0:
                    return Result(1, "", "invocation deadline exceeded", timed_out=True)
                execution_started()
                emit("started", artifact_id=artifact.id, artifact_sha256=artifact.digest, profile=profile,
                     model=endpoint.model, tools=sorted(tools))
                def captured(call_id, observation):
                    # Keep normalized evidence before a recorder/callback can
                    # fail. This is independent of durable persistence success.
                    backend._captured_usage=dict(observation['usage'])
                    if _turn_capture is not None:
                        _turn_capture.record_provider_observation(call_id,observation)
                capture = Capture(_recorder, _context, artifact, profile, endpoint.provider,
                    on_observation=captured) if _recorder is not None else None
                if capture is not None:
                    _capture_ref(capture)
                backend_type = _BoundedAnthropicBackend if endpoint.provider == 'anthropic' else _BoundedBackend
                backend = backend_type(endpoint, policy.max_input_bytes, custom,
                                       started + policy.timeout_s, capture, _budget,
                                       _operation_context, _tool_journal, cancelled,
                                       terminal_capture=_terminal_capture, request_headers=_request_headers,
                                       effort_trace=_effort_trace, effort_profile=profile,
                                       requested_effort=effort, configured_effort=api_default_effort(endpoint.features))
                backend.accounting_context = _context
                request = Invocation(str(Path(cwd).resolve()) if cwd is not None else None,
                                     prompt, invocation_env, remaining, {"prompt_metadata": metadata})
                from .tool_event_contract import current_bindings
                citation_enabled = current_bindings() is not None and current_bindings().citations is not None
                if _turn_prepared is None:
                    from .events import withheld_text
                    with withheld_text(citation_enabled):
                        if citation_enabled:
                            from .openai_compatible import _feature_enabled
                            result = backend._run_prompt_with_tools(request, metadata,
                                _feature_enabled(endpoint.features, 'streaming', default=True))
                        else:
                            result = backend.run_prompt(request)
                else:
                    from .openai_compatible import _feature_enabled
                    backend.turn_mode = True
                    backend.context_guard = _turn_prepared.get('_contextGuard')
                    backend.turn_capture = _turn_capture
                    _turn_capture.backend = backend
                    _turn_capture.parallel_order = policy.parallel_tools is not None
                    if _turn_prepared.get('toolRequestCorrection') is True:  # Sealed in the prepared turn.
                        from .tool_request_correction import ToolRequestCorrection
                        backend.tool_request_correction = _turn_prepared['_toolRequestCorrection'] = (
                            ToolRequestCorrection(_budget))
                    ruleset = _turn_prepared.get('_ruleSet')
                    answer_rules = False
                    if ruleset is not None and any(binding.bound for binding in ruleset.bindings):
                        from .rule_hooks import RuleHooks
                        backend.rule_hooks = _turn_prepared['_ruleHooks'] = RuleHooks(
                            ruleset, message=_turn_prepared['message'], input_data=_turn_prepared['inputData'],
                            authoritative_input_data=_turn_prepared.get('authoritativeInputData'),
                            budget=_budget, check_boundary=backend.check_boundary,
                            retries=_turn_prepared.get('rules', {}).get('ruleRetries', 1),  # Sealed when not 1.
                            typed_result_tools=tuple(name for name, tool in custom.items() if tool.result_contract is not None))
                        answer_rules = any(b.bound for b in ruleset.at('before_answer') + ruleset.at('final'))
                    from .events import withheld_text
                    # A draft that answer-stage rules may refuse is never previewed or streamed early.
                    with withheld_text(answer_rules or citation_enabled):
                        result = backend._run_prompt_with_tools(request, metadata,
                            _feature_enabled(endpoint.features, 'streaming', default=True),
                            initial_messages=_turn_prepared['messages'],
                            execution_options={'compact_tool_results': False}, turn_capture=_turn_capture)
                # The OpenAI text-only path returns Result directly and bypasses
                # _post_chat_turn. Account its terminal usage once, after Capture
                # has persisted the response context, preserving the returned text.
                if (_budget.enabled or backend.execution_budgets) and not backend.turns and (
                        result.exit_code == 0 or 'token_usage_details' in result.metadata or
                        (policy.max_reported_tokens is not None and _budget.provider_requests) or
                        (backend.execution_budgets and backend._provider_attempts)):
                    try:
                        backend.record_turn_usage(result.metadata.get('token_usage_details'))
                    except InvocationBudgetExceeded as exc:
                        result.exit_code = 1
                        result.stderr = str(exc)
                        result.metadata['failure_reason'] = exc.reason
                if backend.turns:
                    result.token_usage = backend.reported_token_usage if backend.usage_complete else None
                    if endpoint.provider == 'anthropic' or features['tool_calls'] or policy.max_reported_tokens is not None:
                        result.metadata['reported_token_usage'] = backend.reported_token_usage
                elif result.exit_code == 0:
                    result.token_usage = _reported_usage(result.metadata.get('token_usage_details'))
                result.metadata['token_usage_status'] = 'unknown' if result.token_usage is None else 'reported'
                if result.exit_code and backend.turns:
                    result.metadata['usage_scope'] = 'reported_completed_turns'
                if result.exit_code == 0 and terminal_tools and _terminal_capture.evidence is None:
                    result.exit_code = 1
                    result.stderr = 'host terminal completion is required'
                    result.metadata['failure_reason'] = 'terminal_completion_missing'
                if (result.exit_code == 0 and result.metadata.get("finish_reason") != "stop"
                        and _terminal_capture.evidence is None):
                    result.exit_code = 1
                    result.stderr = "endpoint did not confirm a complete response"
                    result.metadata["failure_reason"] = "incomplete_response"
                result.metadata.update(artifact_id=artifact.id, artifact_sha256=artifact.digest, profile=profile,
                                       cost_status="unavailable")
                if capture is not None:
                    result.metadata['accounting_v1'] = {'context': _context.to_dict(), 'namespace': _recorder.namespace,
                        'environment': _recorder.environment, 'provider_call_ids': list(capture.call_ids)}
                if acquisition is not None:
                    result.metadata.update(acquisition_id=acquisition.id, acquisition_sha256=acquisition.digest)
                if (result.exit_code != 0 and not result.timed_out
                        and not result.metadata.get('failure_reason') and backend._prework_capacity_failure):
                    result.metadata['prework_failure_v1'] = {
                        'safe_to_fallback': True, 'reason': backend._prework_capacity_failure}
                emit("completed", exit_code=result.exit_code, token_usage=result.token_usage)
                if _terminal_capture.evidence is not None:
                    backend.check_boundary()
                return result
            except EvidenceUseDenied:
                return Result(1, '', 'evidenceUseDenied', metadata={'failure_reason': 'evidenceUseDenied'})
            except ToolResultValidationError:
                return Result(1, '', 'invalidToolResult', metadata={'failure_reason': 'invalidToolResult'})
            except TurnOutputExceeded:
                return Result(1, '', 'outputLimit', metadata={'failure_reason': 'outputLimit'})
            except InvalidToolRequest:
                return Result(1, '', 'invalidToolRequest',
                    token_usage=backend.reported_token_usage if backend and backend.usage_complete else None,
                    metadata={'failure_reason': 'invalidToolRequest'})
            except ToolEffectFailure as exc:
                return Result(1, '', exc.reason,
                    token_usage=backend.reported_token_usage if backend and backend.usage_complete else None,
                    metadata={'failure_reason': exc.reason, 'usage_scope': 'reported_completed_turns',
                              'token_usage_status': 'reported' if backend and backend.usage_complete and backend.reported_token_usage is not None else 'unknown',
                              'reported_token_usage': backend.reported_token_usage if backend else None,
                              'token_usage_details': backend.reported_usage_details if backend else {}})
            except Cancelled:
                return Result(130, "", 'invocation cancelled', token_usage=backend.reported_token_usage if backend and backend.usage_complete else None,
                              metadata={"failure_reason": "cancelled", 'usage_scope': 'reported_completed_turns'})
            except subprocess.TimeoutExpired:
                return Result(1, '', 'Prosaic inspection timed out', timed_out=True,
                              metadata={'failure_reason': 'inspection_timeout'})
            except ToolDeadlineExceeded:
                return Result(1, '', 'invocation deadline exceeded', timed_out=True,
                              token_usage=backend.reported_token_usage if backend and backend.usage_complete else None,
                              metadata={'failure_reason': 'invocation_timeout', 'usage_scope': 'reported_completed_turns'})
            except ContextExecutionFailure as exc:
                return Result(1, '', exc.code,
                    token_usage=backend.reported_token_usage if backend and backend.usage_complete else None,
                    metadata={'failure_reason': exc.code})
            except _TurnContextExceeded:
                return Result(1, '', 'contextLimit',
                    token_usage=backend.reported_token_usage if backend and backend.usage_complete else None,
                    metadata={'failure_reason': 'contextLimit'})
            except LimitExceeded as exc:
                return Result(1, "", str(exc), token_usage=backend.reported_token_usage if backend and backend.usage_complete else None,
                              metadata={"failure_reason": "budget_exceeded", 'usage_scope': 'reported_completed_turns'})
            except InvocationBudgetExceeded as exc:
                return Result(1, '', str(exc),
                    token_usage=backend.reported_token_usage if backend and backend.usage_complete else None,
                    metadata={'failure_reason': exc.reason, 'usage_scope': 'reported_completed_turns',
                              'token_usage_status': 'reported' if backend and backend.usage_complete and backend.reported_token_usage is not None else 'unknown',
                              'reported_token_usage': backend.reported_token_usage if backend else None,
                              'token_usage_details': backend.reported_usage_details if backend else {}})
            finally:
                if backend is not None and backend._dispatch_reservation is not None and not backend._dispatch_reservation.settled:
                    usage=backend._captured_usage
                    if usage is not None and usage.get('status')=='reported':
                        details={dst:usage[src] for src,dst in (('input_tokens','prompt_tokens'),
                            ('output_tokens','completion_tokens'),('total_tokens','total_tokens')) if usage.get(src) is not None}
                        try:backend.record_turn_usage(details)
                        except InvocationBudgetExceeded:pass
                    else:
                        # An unresolved transport or incomplete observation cannot
                        # release its reservation or become zero paid usage.
                        try: record_shared_usage(backend.execution_budgets,None,reservation=backend._dispatch_reservation)
                        except InvocationBudgetExceeded: pass
                        backend.usage_complete=False
                        if backend.budget is not None: backend.budget.usage_complete=False
