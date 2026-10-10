"""Public Prosaic-only invocation API."""
from dataclasses import replace
from contextlib import contextmanager
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
from .events import Cancelled, check_cancelled, event_context, emit
from .openai_compatible import OpenAICompatibleBackend
from .anthropic import AnthropicBackend
from .admission import validate_execution_artifact
from .policy import RunPolicy, requested_tools, BUILTIN_TOOLS
from .types import Invocation, Result
from .tools import validate_custom_tools, custom_descriptors, ToolDeadlineExceeded, ToolExecutionError
from .cli_tools import load_cli_tools, cli_tool_context
from .tool_registry import BoundedToolRegistry
from .accounting import AccountingError, resolve_context
from .accounting_capture import Capture, InvalidEvidence, strict_json
from .http_bounds import LimitExceeded, set_response_timeout, BoundedNativeStream
from .operation_context import InvocationScope
from .tool_effects import admit_tool_effects, ToolEffectFailure
from .telemetry import ObserverEmitter, observation_context, observe, execution_started
from .budgets import InvocationBudget, InvocationBudgetExceeded


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _reported_usage(details):
    if not isinstance(details, dict) or any(type(v) is not int or v < 0 for v in details.values()):
        return None
    return details.get('total_tokens')


class _BoundedStream:
    def __init__(self, response, limit):
        self.response = response
        self.remaining = limit

    def __getattr__(self, name):
        return getattr(self.response, name)

    def readline(self):
        check_cancelled()
        line = self.response.readline(self.remaining + 1)
        self.remaining -= len(line)
        if self.remaining < 0:
            raise LimitExceeded("response exceeds max_response_bytes")
        return line


class _BoundedMixin:
    def __init__(self, endpoint, max_input_bytes, custom_tools, deadline, capture=None, budget=None,
                 scope=None, tool_journal=None, cancelled=None):
        super().__init__(endpoint)
        self.max_input_bytes = max_input_bytes
        self.reported_token_usage = None
        self.usage_complete = True
        self.turns = 0
        self.custom_tools = custom_tools
        self.deadline = deadline
        self.capture = capture
        self.budget = budget
        self.scope, self.tool_journal = scope, tool_journal
        self.cancelled = cancelled if cancelled is not None else lambda: False
        self.tool_call_index = 0
        self.reported_usage_details = {}
        self._turn_usage_unknown = False
        self._latest_usage_complete = None
        self._previous_usage_details = {}

    def check_boundary(self):
        check_cancelled()
        from .conformance import _check_suite_deadline
        _check_suite_deadline()
        if time.monotonic() >= self.deadline:
            raise ToolDeadlineExceeded('invocation deadline exceeded')

    def make_registry(self, cwd, features, metadata):
        return BoundedToolRegistry(super().make_registry(cwd, features, metadata),
                                   self.custom_tools, metadata.get('allowed_tools', ()), self.check_boundary,
                                   self.budget.before_tool if self.budget is not None else None,
                                   self.tool_execution_options)

    def tool_execution_options(self):
        index = self.tool_call_index
        self.tool_call_index += 1
        return dict(scope=self.scope, deadline=self.deadline, cancelled=self.cancelled,
                    call_index=index, tool_journal=self.tool_journal)

    def usage_details(self, parsed):
        details = super().usage_details(parsed)
        # Compatibility parsers omit invalid quantities. A strict cap must see
        # those fields before omission could turn them into trusted evidence.
        if self.budget is not None and self.budget.policy.max_reported_tokens is not None:
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
        if self.budget is not None and self.budget.policy.max_reported_tokens is not None:
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
        if self.budget is not None:
            self.budget.record_usage(usage)

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

    @contextmanager
    def open_http(self, request, *, timeout):
        self.check_boundary()
        if self.budget is not None:
            self.budget.before_provider()
        self._turn_usage_unknown = False
        self._latest_usage_complete = None
        self._previous_usage_details = {}
        started = time.monotonic()
        self._stream_timed_out = False
        observe('provider_request_started')
        outcome = 'execution_failure'
        status = None
        opener = urllib.request.build_opener(_NoRedirect())
        try:
            # A conformance observer can consume the remaining suite time.
            # Recheck its absolute deadline after the callback, before dispatch.
            from .conformance import _check_suite_deadline
            _check_suite_deadline()
            response = (self.capture.open(opener, request, timeout) if self.capture is not None
                        else opener.open(request, timeout=timeout))
            with response:
                status = response.status
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
            raise
        finally:
            observe('provider_request_completed', outcome=outcome, http_status=status,
                    duration_ms=(time.monotonic() - started) * 1000)

    def read_response(self, response):
        self.check_boundary()
        limit = self._config.max_response_bytes
        if self._config.provider == 'anthropic' and callable(getattr(response, 'read1', None)):
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
        if self._config.provider == 'anthropic':
            response = BoundedNativeStream(response, self._config.max_response_bytes, min(deadline, self.deadline))
        elif not isinstance(response, _BoundedStream):
            response = _BoundedStream(response, self._config.max_response_bytes)
        turn = super()._read_sse_turn(response, deadline)
        # Readers may convert transport/deadline timeouts into Result instead
        # of raising. Carry that state to the still-active HTTP observation.
        if isinstance(turn, Result) and turn.timed_out:
            self._stream_timed_out = True
        return turn

    def validate_payload(self, payload):
        check_cancelled()
        if len(json.dumps(payload).encode()) > self.max_input_bytes:
            raise LimitExceeded("conversation exceeds max_input_bytes")

    def _post_chat_turn(self, payload, request, deadline, streaming):
        check_cancelled()
        if time.monotonic() >= deadline:
            return Result(1, "", "invocation deadline exceeded", timed_out=True)
        self.validate_payload(payload)
        attempted_before = self.budget.provider_requests if self.budget is not None else 0
        turn = super()._post_chat_turn(payload, request, deadline, streaming)
        native_failure = isinstance(turn, Result) and self._config.provider == 'anthropic'
        strict_attempt_failure = (isinstance(turn, Result) and self.budget is not None and
            self.budget.policy.max_reported_tokens is not None and
            self.budget.provider_requests > attempted_before)
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
    capabilities = frozenset({'conformance_v1', 'invocation_budgets_v1', 'observer_v1', 'accounting_v1', 'read_receipts_v1', 'initial_tool_v1', 'initial_tool_enforcement_v1', 'acquisition_v1', 'custom_tools_v1', 'cli_tools_v1', 'cli_sandbox_v1'})
    capabilities |= frozenset({'tool_context_v1', 'tool_journal_v1'})
    def __init__(self, config: RuntimeConfig, *, source=".prosaic", executable="prosaic", custom_tools=None,
                 accounting=None, context_defaults=None):
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

    @property
    def tool_descriptors(self):
        return MappingProxyType(custom_descriptors(self._custom_tools))

    @property
    def cli_tool_names(self):
        return frozenset(self._cli_tools)

    @classmethod
    def from_config(cls, path=None, **kwargs):
        return cls(RuntimeConfig.load(path), **kwargs)

    def _preflight_names(self, names, cwd, policy, env, deadline):
        checks = {}
        for name in sorted(names):
            error = None
            if name not in BUILTIN_TOOLS and name not in self._custom_tools:
                error = 'not_registered'
            elif name not in self.config.allowed_tools or name not in policy.allowed_tools:
                error = 'not_granted'
            elif name in self._cli_tools:
                try:
                    self._cli_tools[name].preflight(cwd, policy, env, deadline, self.config.cli_sandbox)
                except ToolExecutionError as exc:
                    error = exc.code
            else:
                from .policy import READ_TOOLS, WRITE_TOOLS
                if name in READ_TOOLS and not policy.read_roots:
                    error = 'missing_read_roots'
                elif name in WRITE_TOOLS and not policy.write_paths:
                    error = 'missing_write_paths'
            checks[name] = {'status': 'error' if error else 'ok',
                            'message': error or ('registration_available' if name not in self._cli_tools else 'cli_available')}
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

    def run(self, artifact: str | ProsaicArtifact, arguments: str = "", *,
            cwd: str | Path = '.', policy: RunPolicy | None = None,
            on_event=None, cancelled=None, env=None,
            acquisition: str | ProsaicArtifact | None = None, context=None, accounting=None,
            observer=None, operation_context: InvocationScope | None = None, tool_journal=None) -> Result:
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
                    context=context, accounting=accounting, operation_context=scope, tool_journal=tool_journal)
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
            operation_context=None, tool_journal=None) -> Result:
        policy = policy or RunPolicy(timeout_s=self.config.limits.timeout_s,
                                     max_tool_rounds=self.config.limits.max_tool_rounds)
        budget = InvocationBudget(policy)
        recorder = accounting if accounting is not None else self.accounting
        enabled = recorder is not None or context is not None or self.context_defaults is not None
        resolved = resolve_context(context, getattr(recorder, 'defaults', None) or self.context_defaults) if enabled else None
        captures = []
        try:
            result = self._run(artifact, arguments, _recorder=recorder, _context=resolved,
                               _capture_ref=captures.append, _budget=budget, cwd=cwd, policy=policy, on_event=on_event,
                               cancelled=cancelled, env=env, acquisition=acquisition,
                               _operation_context=operation_context, _tool_journal=tool_journal)
        except AccountingError:
            result = Result(1, '', 'accounting persistence failed; do not retry the provider automatically',
                            metadata={'failure_reason': 'accounting_failed'})
        if enabled:
            # Capture lineage is supplied by the result when a request began;
            # failures before capture still expose the resolved attribution.
            result.metadata.setdefault('accounting_v1', {'context': resolved.to_dict(),
                'namespace': getattr(recorder, 'namespace', None), 'environment': getattr(recorder, 'environment', None),
                'provider_call_ids': list(captures[0].call_ids) if captures else []})
        if budget.enabled:
            result.metadata['invocation_budgets_v1'] = budget.metadata()
        return result

    def _run(self, artifact: str | ProsaicArtifact, arguments: str = "", *,
            cwd: str | Path = ".", policy: RunPolicy | None = None,
            on_event=None, cancelled=None, env=None,
            acquisition: str | ProsaicArtifact | None = None, _recorder=None, _context=None, _capture_ref=None,
            _budget=None, _operation_context=None, _tool_journal=None) -> Result:
        policy = policy or RunPolicy(timeout_s=self.config.limits.timeout_s,
                                     max_tool_rounds=self.config.limits.max_tool_rounds)
        started = time.monotonic()
        backend = None
        invocation_env = dict(os.environ if env is None else env)
        with event_context(on_event, cancelled), cli_tool_context(
            cwd, policy, invocation_env, started + policy.timeout_s, self.config.cli_sandbox):
            try:
                check_cancelled()
                if isinstance(artifact, str):
                    artifact = inspect_artifact(artifact, self.source, executable=self.executable,
                                                timeout_s=min(30, policy.timeout_s))
                if not isinstance(artifact, ProsaicArtifact):
                    raise TypeError("artifact must be a Prosaic artifact identifier or inspection artifact")
                artifact = ProsaicArtifact.from_inspection({
                    "id": artifact.id, "type": artifact.type, "frontmatter": artifact.frontmatter,
                    "body": artifact.body, "resources": list(artifact.resources)})
                validate_execution_artifact(artifact, self.config)
                requested = requested_tools(artifact.frontmatter.get("tools"))
                admit_tool_effects(self._custom_tools, requested, _operation_context, _tool_journal)
                unsupported = requested - BUILTIN_TOOLS - self._custom_tools.keys()
                if unsupported:
                    raise ValueError(f"unsupported tools: {sorted(unsupported)}")
                cli_requested = requested & self._cli_tools.keys()
                if cli_requested:
                    report = self._preflight_names(cli_requested, str(Path(cwd).resolve()), policy,
                                                   invocation_env, started + policy.timeout_s)
                    if not report['ok']:
                        failed = {name: check['message'] for name, check in report['checks'].items() if check['status'] != 'ok'}
                        raise ValueError(f'CLI tool preflight failed: {failed}')
                tools = requested & self.config.allowed_tools & frozenset(policy.allowed_tools)
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
                    acquisition = ProsaicArtifact.from_inspection({
                        'id': acquisition.id, 'type': acquisition.type, 'frontmatter': acquisition.frontmatter,
                        'body': acquisition.body, 'resources': list(acquisition.resources)})
                    validate_execution_artifact(artifact, self.config, acquisition=acquisition)
                    acquisition_tools = requested_tools(acquisition.frontmatter.get('tools'))
                    if acquisition_tools - requested or policy.initial_tool not in acquisition_tools:
                        raise ValueError('acquisition requires an explicit initial_tool and tools granted by final prose, runtime and host')
                    acquisition_tools &= tools
                tier = artifact.frontmatter.get("model_tier")
                profile = self.config.routes[tier] if tier is not None else self.config.default_profile
                endpoint = self.config.profiles[profile]
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
                capture = Capture(_recorder, _context, artifact, profile, endpoint.provider) if _recorder is not None else None
                if capture is not None:
                    _capture_ref(capture)
                backend_type = _BoundedAnthropicBackend if endpoint.provider == 'anthropic' else _BoundedBackend
                backend = backend_type(endpoint, policy.max_input_bytes, self._custom_tools,
                                       started + policy.timeout_s, capture, _budget,
                                       _operation_context, _tool_journal, cancelled)
                result = backend.run_prompt(Invocation(
                    str(Path(cwd).resolve()), prompt, invocation_env, remaining,
                    {"prompt_metadata": metadata}))
                # The OpenAI text-only path returns Result directly and bypasses
                # _post_chat_turn. Account its terminal usage once, after Capture
                # has persisted the response context, preserving the returned text.
                if _budget.enabled and not backend.turns and (
                        result.exit_code == 0 or 'token_usage_details' in result.metadata or
                        (policy.max_reported_tokens is not None and _budget.provider_requests)):
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
                if result.exit_code == 0 and result.metadata.get("finish_reason") != "stop":
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
                emit("completed", exit_code=result.exit_code, token_usage=result.token_usage)
                return result
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
