"""Public Prosaic-only invocation API."""
from dataclasses import replace
from pathlib import Path
import os
import time
import json

from .artifacts import ProsaicArtifact, inspect_artifact
from .config import RuntimeConfig
from .events import Cancelled, check_cancelled, event_context, emit
from .openai_compatible import OpenAICompatibleBackend
from .policy import RunPolicy, requested_tools, BUILTIN_TOOLS
from .types import Invocation, Result


class LimitExceeded(ValueError):
    pass


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
    def __init__(self, endpoint, max_input_bytes):
        super().__init__(endpoint)
        self.max_input_bytes = max_input_bytes

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

    def _post_chat_turn(self, payload, request, deadline, streaming):
        check_cancelled()
        if time.monotonic() >= deadline:
            return Result(1, "", "invocation deadline exceeded", timed_out=True)
        if len(json.dumps(payload).encode()) > self.max_input_bytes:
            raise LimitExceeded("conversation exceeds max_input_bytes")
        return super()._post_chat_turn(payload, request, deadline, streaming)


class ProsaicRuntime:
    def __init__(self, config: RuntimeConfig, *, source=".prosaic", executable="prosaic"):
        self.config = config
        self.source = Path(source)
        self.executable = executable

    @classmethod
    def from_config(cls, path, **kwargs):
        return cls(RuntimeConfig.load(path), **kwargs)

    def run(self, artifact: str | ProsaicArtifact, arguments: str = "", *,
            cwd: str | Path = ".", policy: RunPolicy | None = None,
            on_event=None, cancelled=None, env=None) -> Result:
        policy = policy or RunPolicy()
        started = time.monotonic()
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
                if requested - BUILTIN_TOOLS:
                    raise ValueError(f"unsupported tools: {sorted(requested - BUILTIN_TOOLS)}")
                tools = requested & self.config.allowed_tools & frozenset(policy.allowed_tools)
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
                effort = artifact.frontmatter.get("effort")
                if effort is not None:
                    if effort not in {"low", "medium", "high"}:
                        raise ValueError(f"unsupported effort: {effort}")
                    metadata["effort"] = effort
                prompt = artifact.render(arguments)
                if len(prompt.encode()) > policy.max_input_bytes:
                    raise ValueError("artifact and arguments exceed max_input_bytes")
                remaining = policy.timeout_s - (time.monotonic() - started)
                if remaining <= 0:
                    return Result(1, "", "invocation deadline exceeded", timed_out=True)
                emit("started", artifact_id=artifact.id, artifact_sha256=artifact.digest, profile=profile,
                     model=endpoint.model, tools=sorted(tools))
                result = _BoundedBackend(endpoint, policy.max_input_bytes).run_prompt(Invocation(
                    str(Path(cwd).resolve()), prompt, dict(os.environ if env is None else env), remaining,
                    {"prompt_metadata": metadata}))
                if result.exit_code == 0 and result.metadata.get("finish_reason") != "stop":
                    result.exit_code = 1
                    result.stderr = "endpoint did not confirm a complete response"
                    result.metadata["failure_reason"] = "incomplete_response"
                result.metadata.update(artifact_id=artifact.id, artifact_sha256=artifact.digest, profile=profile,
                                       cost_status="unavailable")
                emit("completed", exit_code=result.exit_code, token_usage=result.token_usage)
                return result
            except Cancelled as exc:
                return Result(130, "", str(exc), metadata={"failure_reason": "cancelled"})
            except LimitExceeded as exc:
                return Result(1, "", str(exc), metadata={"failure_reason": "budget_exceeded"})
