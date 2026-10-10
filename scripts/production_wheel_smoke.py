"""Isolated installed acceptance, using finite owned fixtures and public SDK APIs.

The SQLite implementation is an explicitly loaded test fixture shipped in the
source archive. It is not an SDK journal or a production usage ledger.
"""
import faulthandler
import sys

if sys.argv[1:2] == ['--worker']:
    # Include imports and ownership checks in diagnostics for the separately
    # bounded child. The parent still enforces its original 30-second limit.
    faulthandler.dump_traceback_later(10, repeat=True)

from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import metadata, util
import json
import os
from pathlib import Path
import subprocess
import tempfile
from threading import Thread

import prosaic
import prosaic_runtime as runtime
import prosaic_runtime_postgres as postgres
from prosaic_runtime.diagnostics import conformance


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/support/tool_journal.py"
SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


def installed_origins():
    assert sys.flags.isolated == 1, "run this script with python -I"
    installed = {d.metadata["Name"].lower().replace("_", "-") for d in metadata.distributions()}
    assert not {"prosaic", "prosaic-runtime", "prosaic-runtime-postgres"} & installed
    owners = {}
    origins = {}
    for package, name, version in ((prosaic, "b3-prosaic", "0.4.0"),
            (runtime, "b3-prosaic-runtime", "0.8.0"),
            (postgres, "b3-prosaic-runtime-postgres", "0.2.0")):
        owner = metadata.distribution(name)
        assert owner.version == package.__version__ == version
        assert metadata.packages_distributions()[package.__name__] == [name]
        owners[package.__name__] = {Path(owner.locate_file(file)).resolve() for file in owner.files}
    for name, module in tuple(sys.modules.items()):
        family = name.split(".")[0]
        if family in owners and getattr(module, "__file__", None):
            origin = Path(module.__file__).resolve()
            assert "site-packages" in origin.parts and origin in owners[family], (name, origin)
            origins[name] = str(origin)
    return origins


def journal(path):
    spec = util.spec_from_file_location("production_acceptance_journal_fixture", FIXTURE)
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SqliteJournal(path)


def artifact(tools=()):
    return runtime.ProsaicArtifact.from_inspection({"id": "acceptance/synthetic.md",
        "type": "subagent", "frontmatter": {"tools": list(tools)},
        "body": "Return the synthetic result for {{args}}."})


def completion(content="done", name=None):
    message = {"content": content}
    if name is not None:
        message["tool_calls"] = [{"id": "fixture-call", "type": "function",
            "function": {"name": name, "arguments": "{}"}}]
    return {"model": "synthetic", "choices": [{"message": message,
        "finish_reason": "tool_calls" if name else "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}


class ProviderFixture:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(2)
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                assert 0 < length <= 65536
                fixture.requests.append(json.loads(self.rfile.read(length)))
                if not fixture.replies:
                    self.send_error(500, "fixture exhausted")
                    return
                body = json.dumps(fixture.replies.pop(0)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.http.serve_forever, daemon=True)
    def __enter__(self):
        self.thread.start()
        return self
    def __exit__(self, *args):
        self.http.shutdown()
        self.thread.join(timeout=2)
        self.http.server_close()
    def config(self, tools=()):
        endpoint = runtime.EndpointConfig(f"http://127.0.0.1:{self.http.server_port}/v1",
            "synthetic", features={"streaming": False}, max_response_bytes=65536)
        return runtime.RuntimeConfig({"fixture": endpoint}, {}, "fixture", frozenset(tools))


def failing_observer(record):
    raise RuntimeError("PRIVATE_OBSERVER_FAILURE")


def worker(directory, attempt):
    installed_origins()
    os.environ["PATH"] = ""
    directory = Path(directory)
    effects = directory / "effects.txt"
    dispatched = []
    def effect(args, context):
        assert context.scope.operation_namespace == "synthetic-tenant"
        assert context.operation_key == "business-order-1"
        dispatched.append(context.operation_key)
        with effects.open("a") as stream:
            stream.write(context.operation_key + "\n")
        return {"accepted": True}
    tool = runtime.CustomTool("accept_order", "Accept a synthetic order", SCHEMA,
        effect, "v1", with_context=True,
        operation_key=lambda args, context: "business-order-1")
    store = journal(directory / "journal.sqlite")
    assert runtime.tool_journal_descriptor(store) == {
        "contract_version": "tool-journal-v1", "identity": "synthetic-journal"}
    with ProviderFixture([completion("", "accept_order"), completion()]) as provider:
        result = runtime.ProsaicRuntime(provider.config(["accept_order"]),
            custom_tools={"accept_order": tool}).run(artifact(["accept_order"]),
            cwd=directory, tool_journal=store, observer=failing_observer,
            operation_context=runtime.InvocationScope(attempt, operation_namespace="synthetic-tenant"),
            policy=runtime.RunPolicy(timeout_s=5, allowed_tools=frozenset({"accept_order"}),
                max_provider_requests=2, max_tool_calls=1, max_reported_tokens=14))
        assert result.exit_code == 0 and result.stdout == "done" and result.token_usage == 14
        assert len(provider.requests) == 2 and not provider.replies
        outcome = next(m for m in provider.requests[1]["messages"] if m["role"] == "tool")
        assert json.loads(outcome["content"]) == {"status": "ok", "result": {"accepted": True}}
        assert "accounting_v1" not in result.metadata
        assert result.metadata["invocation_budgets_v1"]["tool_calls"] == 1
        print(json.dumps({"http_requests": 2, "handler_calls": len(dispatched),
                          "reported_tokens": 14, "origins": installed_origins()}))


def main():
    installed_origins()
    for name in ("InvocationScope", "ObserverEmitter", "ToolExecutionContext", "ToolClaim",
                 "ToolJournal", "tool_journal_descriptor", "evaluate_conformance"):
        assert callable(getattr(runtime, name)) and name in runtime.__all__
    os.environ["PATH"] = ""
    with tempfile.TemporaryDirectory(prefix="runtime-wheel-acceptance-") as temporary:
        directory = Path(temporary)
        workers = []
        for attempt in ("first", "replay"):
            try:
                process = subprocess.run([sys.executable, "-I", str(Path(__file__).resolve()),
                    "--worker", temporary, attempt], cwd=temporary, env=dict(os.environ),
                    capture_output=True, text=True, timeout=30)
            except subprocess.TimeoutExpired as error:
                for captured in (error.stdout, error.stderr):
                    if captured:
                        text = captured.decode('utf-8', errors='replace') if isinstance(captured, bytes) else captured
                        print(text, file=sys.stderr, end='')
                raise
            assert process.returncode == 0, process.stdout + process.stderr
            workers.append(json.loads(process.stdout))
        assert [row["handler_calls"] for row in workers] == [1, 0]
        assert (directory / "effects.txt").read_text() == "business-order-1\n"
        legacy_calls, observations = [], []
        def observer(record):
            observations.append(dict(record))
            failing_observer(record)
        legacy = runtime.CustomTool("legacy_lookup", "Legacy one-argument handler", SCHEMA,
            lambda args: legacy_calls.append(dict(args)) or {"found": True}, "v1")
        assert legacy.descriptor == {"name": "legacy_lookup", "description": "Legacy one-argument handler",
            "parameters": SCHEMA, "version": "v1", "max_argument_bytes": 16384,
            "max_result_bytes": 65536, "authorization_required": False}
        with ProviderFixture([completion("", "legacy_lookup"), completion(), completion()]) as provider:
            executor = runtime.ProsaicRuntime(provider.config(["legacy_lookup"]),
                custom_tools={"legacy_lookup": legacy})
            result = executor.run(artifact(["legacy_lookup"]), cwd=temporary, observer=observer,
                operation_context=runtime.InvocationScope("legacy"),
                policy=runtime.RunPolicy(timeout_s=5, allowed_tools=frozenset({"legacy_lookup"})))
            assert result.exit_code == 0 and result.token_usage == 14 and legacy_calls == [{}]
            assert "accounting_v1" not in result.metadata and "invocation_budgets_v1" not in result.metadata
            assert set(asdict(result)) == {"exit_code", "stdout", "stderr", "token_usage", "cost_usd", "timed_out", "metadata"}
            denied = executor.run(artifact(), cwd=temporary, observer=observer,
                policy=runtime.RunPolicy(timeout_s=5, max_provider_requests=0))
            assert denied.metadata["failure_reason"] == "provider_request_limit"
            assert len(provider.requests) == 2
            capped = executor.run(artifact(), cwd=temporary, observer=observer,
                policy=runtime.RunPolicy(timeout_s=5, max_reported_tokens=6))
            assert capped.metadata["failure_reason"] == "token_limit" and capped.token_usage == 7
            assert len(provider.requests) == 3 and not provider.replies
            offline = conformance(provider.config(), "fixture", live=False)
            assert offline["qualification"] == "not_qualified"
            assert all(case["state"] == "not_run" for case in offline["cases"])
            assert len(provider.requests) == 3
            fingerprint = offline["profileFingerprint"]
            assertion = {"text_complete": {"state": "passed", "reason": "complete",
                "suiteId": "text-tools-v1", "profileFingerprint": fingerprint,
                "origin": "fixture", "providerRequests": 2, "reportedTokens": 14}}
            report = runtime.evaluate_conformance("text-tools-v1", fingerprint, assertion)
            assert report == runtime.evaluate_conformance("text-tools-v1", fingerprint, assertion)
            assert report["qualification"] == "not_qualified"
        assert [r["outcome"] for r in observations if r["event"] == "invocation_completed"] == [
            "completed", "budget_failure", "budget_failure"]
        assert "PRIVATE_OBSERVER_FAILURE" not in json.dumps(observations)
        print(json.dumps({"versions": {"core": prosaic.__version__, "runtime": runtime.__version__,
            "recorder": postgres.__version__}, "origins": installed_origins(),
            "journal_fixture_origin": str(FIXTURE), "worker_http_requests": [2, 2],
            "worker_handler_calls": [1, 0], "parent_http_requests": 3,
            "legacy_handler_calls": 1, "effect_lines": 1, "offline_conformance": report,
            "database_qualified": False, "live_provider_qualified": False}, sort_keys=True))


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        worker(sys.argv[2], sys.argv[3])
    else:
        assert len(sys.argv) == 1
        main()
