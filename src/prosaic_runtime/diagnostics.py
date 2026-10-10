"""Explicit endpoint diagnostics and bounded live smoke checks."""
from pathlib import Path
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

from .config import RuntimeConfig
from .openai_compatible import _api_key
from .anthropic import native_headers
from .policy import RunPolicy
from .runtime import ProsaicRuntime

SMOKE_SOURCE = Path(__file__).parent / "smoke_prose"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward endpoint credentials to redirect destinations.
        return None


def run_check(config, profile, *, with_tools=False, timeout_s=None, max_tool_rounds=None, on_event=None):
    """Use bundled neutral prose and synthetic evidence, never caller files."""
    endpoint = config.profiles[profile]
    name = 'reviewer' if with_tools else 'native-summarizer' if endpoint.provider == 'anthropic' else 'summarizer'
    grants = frozenset({"read_file"}) if with_tools else frozenset()
    # Smoke's explicit opt-in authorizes only these synthetic read-only examples.
    isolated = RuntimeConfig({profile: endpoint}, {"fast": profile}, profile, grants, config.limits)
    policy = RunPolicy(allowed_tools=grants, read_roots=("evidence",) if with_tools else (),
                       timeout_s=timeout_s if timeout_s is not None else config.limits.timeout_s,
                       max_tool_rounds=max_tool_rounds if max_tool_rounds is not None else config.limits.max_tool_rounds)
    events = []
    def record(event):
        events.append(event)
        if on_event:
            on_event(event)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="prosaic-smoke-") as directory:
        root = Path(directory)
        (root / "evidence").mkdir()
        (root / "evidence/pilot.md").write_text((SMOKE_SOURCE / "pilot.md").read_text(), encoding="utf-8")
        arguments = ("Review evidence/pilot.md. What do we know, and what remains unknown?" if with_tools else
                     "S1: The pilot processed 120 requests. S2: Three requests timed out. S3: The cause has not been established.")
        result = ProsaicRuntime(isolated, source=SMOKE_SOURCE).run(
            f"subagents/{name}.md", arguments, cwd=root, policy=policy, on_event=record)
    streaming = any(e["event"] == "text_delta" for e in events)
    tools = [e for e in events if e["event"] == "tool_completed"]
    tool_ok = any(e["name"] == "read_file" and e["status"] == "ok" for e in tools)
    complete = result.exit_code == 0 and bool(result.stdout.strip())
    # Observed streaming is reported independently; non-streaming endpoints are supported.
    passed = complete and (tool_ok if with_tools else not tools)
    return {"status": "ok" if passed else "failed", "completion": complete,
            "streaming": streaming, "streaming_requested": endpoint.features.get("streaming", True),
            "tool_execution": tool_ok, "tool_events": tools,
            "exit_code": result.exit_code, "elapsed_s": round(time.monotonic() - started, 2),
            "finish_reason": result.metadata.get("finish_reason"), "token_usage": result.token_usage,
            "error": failure_hint(result) if result.exit_code else None}


def failure_hint(result):
    status = result.metadata.get("http_status")
    if status in (401, 403):
        return f"HTTP {status}: authentication rejected; check the configured API key and permissions."
    if status == 404:
        return "HTTP 404: check the API base URL and model identifier."
    if result.timed_out or result.metadata.get("provider_error_code") == "timeout":
        return "Invocation timed out; check server load or increase limits.timeout_s / --timeout."
    if status:
        return f"HTTP {status}: endpoint rejected the request; check model and optional fields."
    return "Execution failed; check endpoint connectivity, response format and invocation limits."


def doctor(config, profile, *, inference=False, timeout_s=None, on_event=None):
    endpoint = config.profiles[profile]
    checks = {"configuration": {"status": "ok", "profile": profile, "model": endpoint.model}}
    try:
        probe = subprocess.run(["prosaic", "--help"], capture_output=True, timeout=10)
        checks["prosaic"] = {"status": "ok" if probe.returncode == 0 else "failed"}
    except (OSError, subprocess.TimeoutExpired):
        checks["prosaic"] = {"status": "failed", "message": "Prosaic CLI is unavailable; install it and check PATH."}
    token, error = _api_key(endpoint.api_key_env, endpoint.api_key_file, os.environ)
    checks["credentials"] = {"status": "failed" if error else "ok", "configured": bool(token)}
    if error:
        checks["credentials"]["message"] = "Unable to load configured credentials."
        checks["discovery"] = {"status": "skipped"}
    else:
        headers = (native_headers(token) if endpoint.provider == 'anthropic'
                   else {"Authorization": f"Bearer {token}"} if token else {})
        request = urllib.request.Request(endpoint.base_url.rstrip("/") + "/models", headers=headers)
        try:
            with urllib.request.build_opener(_NoRedirect).open(request, timeout=min(timeout_s or 15, 15)) as response:
                body = response.read(1_048_577)
                if len(body) > 1_048_576:
                    raise ValueError("oversized discovery response")
                data = json.loads(body)
            models = data.get("data") if isinstance(data, dict) else None
            if not isinstance(models, list):
                raise ValueError("invalid model list")
            found = any(isinstance(m, dict) and m.get("id") == endpoint.model for m in models)
            checks["discovery"] = {"status": "ok" if found else "warning", "model_found": found,
                                   "message": "Model found." if found else "Configured model not listed; direct inference may still work."}
        except urllib.error.HTTPError as exc:
            checks["discovery"] = {"status": "warning" if exc.code in (404, 405, 501) else "failed",
                                   "http_status": exc.code,
                                   "message": "Authentication rejected." if exc.code in (401, 403) else "Model discovery unavailable; no inference attempted by discovery."}
        except (OSError, ValueError):
            checks["discovery"] = {"status": "failed", "message": "Cannot read model discovery; check connectivity, URL and response format."}
    checks["inference"] = {"status": "skipped"}
    if inference and checks["prosaic"]["status"] == "ok" and not error:
        checks["inference"] = run_check(config, profile, timeout_s=timeout_s, on_event=on_event)
    return {"command": "doctor", "ok": not any(c["status"] == "failed" for c in checks.values()), "checks": checks}


def smoke(config, profile, *, timeout_s=None, max_tool_rounds=None, on_event=None):
    tests = {}
    for name, with_tools in (("without_tools", False), ("with_tools", True)):
        tests[name] = run_check(config, profile, with_tools=with_tools, timeout_s=timeout_s,
                               max_tool_rounds=max_tool_rounds, on_event=on_event)
    return {"command": "smoke", "ok": all(t["status"] == "ok" for t in tests.values()), "tests": tests}


def conformance(config, profile, live=False, policy=None, observer=None, *, evidence_origin='live'):
    """Default is pure unexecuted evidence; execution requires explicit opt-in."""
    from .conformance import _profile_fingerprint, evaluate_conformance, _run_suite
    fingerprint = _profile_fingerprint(config.profiles[profile])
    if not live:
        return evaluate_conformance('text-tools-v1', fingerprint, {})
    return _run_suite(config, profile, fingerprint, policy, observer, evidence_origin)
