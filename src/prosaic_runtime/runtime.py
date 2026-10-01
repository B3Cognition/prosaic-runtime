"""Public Prosaic-only invocation API."""
from dataclasses import replace
from pathlib import Path
import os
import time
import json
import subprocess
import urllib.request
from types import MappingProxyType

from .artifacts import ProsaicArtifact, inspect_artifact
from .config import RuntimeConfig
from .events import Cancelled, check_cancelled, event_context, emit
from .openai_compatible import OpenAICompatibleBackend
from .policy import RunPolicy, requested_tools, BUILTIN_TOOLS
from .types import Invocation, Result
from .tools import validate_custom_tools, custom_descriptors, ToolDeadlineExceeded
from .tool_registry import BoundedToolRegistry


class LimitExceeded(ValueError):
    pass


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


class _BoundedBackend(OpenAICompatibleBackend):
    def __init__(self, endpoint, max_input_bytes, custom_tools, deadline):
        super().__init__(endpoint)
        self.max_input_bytes = max_input_bytes
        self.reported_token_usage = None
        self.usage_complete = True
        self.turns = 0
        self.custom_tools = custom_tools
        self.deadline = deadline

    def check_boundary(self):
        check_cancelled()
        if time.monotonic() >= self.deadline:
            raise ToolDeadlineExceeded('invocation deadline exceeded')

    def make_registry(self, cwd, features, metadata):
        return BoundedToolRegistry(super().make_registry(cwd, features, metadata),
                                   self.custom_tools, metadata.get('allowed_tools', ()), self.check_boundary)

    def tool_call_summary(self, tool_call):
        function = tool_call.get('function')
        if isinstance(function, dict) and function.get('name') in self.custom_tools:
            return 'host-registered tool (arguments omitted)'
        return super().tool_call_summary(tool_call)

    def tool_event_metadata(self, name):
        return {'tool_version': self.custom_tools[name].version} if name in self.custom_tools else {}

    def open_http(self, request, *, timeout):
        return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)

    def read_response(self, response):
        check_cancelled()
        body = response.read(self._config.max_response_bytes + 1)
        if len(body) > self._config.max_response_bytes:
            raise LimitExceeded("response exceeds max_response_bytes")
        return body.decode("utf-8", errors="strict")

    def _read_sse_turn(self, response, deadline):
        if not isinstance(response, _BoundedStream):
            response = _BoundedStream(response, self._config.max_response_bytes)
        return super()._read_sse_turn(response, deadline)

    def validate_payload(self, payload):
        check_cancelled()
        if len(json.dumps(payload).encode()) > self.max_input_bytes:
            raise LimitExceeded("conversation exceeds max_input_bytes")

    def _post_chat_turn(self, payload, request, deadline, streaming):
        check_cancelled()
        if time.monotonic() >= deadline:
            return Result(1, "", "invocation deadline exceeded", timed_out=True)
        self.validate_payload(payload)
        turn = super()._post_chat_turn(payload, request, deadline, streaming)
        if not isinstance(turn, Result):
            self.turns += 1
            usage = _reported_usage(turn.token_usage_details)
            if usage is None:
                self.usage_complete = False
            else:
                self.reported_token_usage = (self.reported_token_usage or 0) + usage
        return turn


class ProsaicRuntime:
    capabilities = frozenset({'read_receipts_v1', 'initial_tool_v1', 'initial_tool_enforcement_v1', 'acquisition_v1', 'custom_tools_v1'})
    def __init__(self, config: RuntimeConfig, *, source=".prosaic", executable="prosaic", custom_tools=None):
        self.config = config
        self.source = Path(source)
        self.executable = executable
        self._custom_tools = validate_custom_tools(custom_tools)

    @property
    def tool_descriptors(self):
        return MappingProxyType(custom_descriptors(self._custom_tools))

    @classmethod
    def from_config(cls, path=None, **kwargs):
        return cls(RuntimeConfig.load(path), **kwargs)

    def run(self, artifact: str | ProsaicArtifact, arguments: str = "", *,
            cwd: str | Path = ".", policy: RunPolicy | None = None,
            on_event=None, cancelled=None, env=None,
            acquisition: str | ProsaicArtifact | None = None) -> Result:
        policy = policy or RunPolicy(timeout_s=self.config.limits.timeout_s,
                                     max_tool_rounds=self.config.limits.max_tool_rounds)
        started = time.monotonic()
        backend = None
        with event_context(on_event, cancelled):
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
                requested = requested_tools(artifact.frontmatter.get("tools"))
                unsupported = requested - BUILTIN_TOOLS - self._custom_tools.keys()
                if unsupported:
                    raise ValueError(f"unsupported tools: {sorted(unsupported)}")
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
                    acquisition_tools = requested_tools(acquisition.frontmatter.get('tools'))
                    if acquisition_tools - requested or policy.initial_tool not in acquisition_tools:
                        raise ValueError('acquisition requires an explicit initial_tool and tools granted by final prose, runtime and host')
                    acquisition_tools &= tools
                    for key in ('model_tier', 'effort'):
                        if key in acquisition.frontmatter and acquisition.frontmatter[key] != artifact.frontmatter.get(key):
                            raise ValueError(f'acquisition {key} must match final prose or be omitted')
                tier = artifact.frontmatter.get("model_tier")
                if tier is not None and tier not in self.config.routes:
                    raise ValueError(f"no endpoint route for model_tier: {tier}")
                profile = self.config.routes[tier] if tier is not None else self.config.default_profile
                endpoint = self.config.profiles[profile]
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
                    if effort not in {"low", "medium", "high"}:
                        raise ValueError(f"unsupported effort: {effort}")
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
                emit("started", artifact_id=artifact.id, artifact_sha256=artifact.digest, profile=profile,
                     model=endpoint.model, tools=sorted(tools))
                backend = _BoundedBackend(endpoint, policy.max_input_bytes, self._custom_tools, started + policy.timeout_s)
                result = backend.run_prompt(Invocation(
                    str(Path(cwd).resolve()), prompt, dict(os.environ if env is None else env), remaining,
                    {"prompt_metadata": metadata}))
                if backend.turns:
                    result.token_usage = backend.reported_token_usage if backend.usage_complete else None
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
                if acquisition is not None:
                    result.metadata.update(acquisition_id=acquisition.id, acquisition_sha256=acquisition.digest)
                emit("completed", exit_code=result.exit_code, token_usage=result.token_usage)
                return result
            except Cancelled as exc:
                return Result(130, "", str(exc), token_usage=backend.reported_token_usage if backend and backend.usage_complete else None,
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
