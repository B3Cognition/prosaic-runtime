import io
import json
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
import shutil
import subprocess

import pytest
from prosaic_runtime import EndpointConfig, ProsaicArtifact, ProsaicRuntime, RunPolicy, RuntimeConfig
from prosaic_runtime.artifacts import inspect_artifact
from prosaic_runtime.openai_compatible import _OpenAIToolRegistry
from prosaic_runtime.policy import BUILTIN_TOOLS


def artifact(tools="", **extra):
    return ProsaicArtifact.from_inspection({"id": "subagents/test.md", "type": "subagent",
        "body": "Perform {{args}}.", "frontmatter": {"model_tier": "fast", "tools": tools, **extra}})


def runtime(endpoint=None, **kwargs):
    return ProsaicRuntime(RuntimeConfig(
        {"small": endpoint or EndpointConfig("http://localhost:9/v1", "small", features={"streaming": False})},
        {"fast": "small"}, "small", BUILTIN_TOOLS), **kwargs)


def call(registry, name, **args):
    return json.loads(registry.execute_message({"id": "call-1", "function": {
        "name": name, "arguments": json.dumps(args)}})["content"])


def test_default_registry_grants_no_tools_or_writes(tmp_path):
    registry = _OpenAIToolRegistry(tmp_path, {})
    assert registry.openai_tools() == []
    assert call(registry, "write_file", path="x", content="bad")["status"] == "error"
    assert not (tmp_path / "x").exists()


def test_empty_write_paths_deny_even_granted_tool(tmp_path):
    registry = _OpenAIToolRegistry(tmp_path, {}, {"allowed_tools": ["write_file"]})
    assert call(registry, "write_file", path="x", content="bad")["status"] == "error"
    assert not (tmp_path / "x").exists()


def test_exact_write_paths_and_symlink_escape(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "escape").symlink_to(tmp_path, target_is_directory=True)
    registry = _OpenAIToolRegistry(root, {}, {"allowed_tools": ["write_file"], "tool_write_paths": ["ok"]})
    assert call(registry, "write_file", path="ok", content="yes")["status"] == "ok"
    assert call(registry, "write_file", path="other", content="bad")["status"] == "error"
    assert call(registry, "write_file", path="escape/out", content="bad")["status"] == "error"
    assert not (tmp_path / "out").exists()


def test_read_scope_and_forbidden_scope(tmp_path):
    (tmp_path / "ok").write_text("readable")
    (tmp_path / "private").write_text("private")
    registry = _OpenAIToolRegistry(tmp_path, {}, {"allowed_tools": ["read_file"],
        "tool_read_roots": ["."], "tool_forbidden_roots": ["private"]})
    assert call(registry, "read_file", path="ok")["status"] == "ok"
    assert call(registry, "read_file", path="private")["status"] == "error"
    denied = _OpenAIToolRegistry(tmp_path, {}, {"allowed_tools": ["read_file"]})
    assert call(denied, "read_file", path="ok")["status"] == "error"


@pytest.mark.parametrize("tools", ["full", "shell", ["bash"]])
def test_unsupported_capabilities_fail_before_http(tools):
    with pytest.raises(ValueError):
        runtime().run(artifact(tools))


def test_unknown_model_tier_does_not_silently_downgrade():
    with pytest.raises(ValueError, match="no endpoint route"):
        runtime().run(artifact(model_tier="strong"))


def test_input_budget_before_http():
    with pytest.raises(ValueError, match="max_input_bytes"):
        runtime().run(artifact(), "x" * 50, policy=RunPolicy(max_input_bytes=10))


def test_cancel_before_network():
    result = runtime().run(artifact(), cancelled=lambda: True)
    assert result.exit_code == 130
    assert result.metadata["failure_reason"] == "cancelled"


def test_inspection_preserves_bundle_and_rejects_escape():
    data = {"id": "subagents/test.md", "type": "subagent", "frontmatter": {}, "body": "Body",
            "resources": [{"relPath": "reference.md", "content": "Reference"}]}
    assert "Reference" in ProsaicArtifact.from_inspection(data).render()
    data["resources"][0]["relPath"] = "../outside"
    with pytest.raises(ValueError):
        ProsaicArtifact.from_inspection(data)


@pytest.fixture
def server():
    requests = []
    responses = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            content_type, body = responses.pop(0)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}/v1", requests, responses
    finally:
        http.shutdown()
        thread.join(timeout=2)
        http.server_close()


def completion(content="done", calls=None):
    message = {"content": content}
    if calls:
        message["tool_calls"] = calls
    return ("application/json", json.dumps({"choices": [{"message": message,
        "finish_reason": "tool_calls" if calls else "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}).encode())


def test_real_http_tool_loop_enforces_intersection(server, tmp_path, capsys):
    url, requests, responses = server
    (tmp_path / "input").write_text("source evidence")
    responses.extend([completion("", [
        {"id": "read", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"input"}'}},
        {"id": "write", "type": "function", "function": {"name": "write_file", "arguments": '{"path":"bad","content":"oops"}'}},
    ]), completion()])
    events = []
    result = runtime(EndpointConfig(url, "small", features={"streaming": False})).run(
        artifact("write"), "summary", cwd=tmp_path,
        policy=RunPolicy(allowed_tools=frozenset({"read_file"}), read_roots=(".",)), on_event=events.append)
    assert result.exit_code == 0
    assert result.token_usage == 14
    assert requests[0]["model"] == "small"
    assert [tool["function"]["name"] for tool in requests[0]["tools"]] == ["read_file"]
    assert "source evidence" in requests[1]["messages"][3]["content"]
    assert "Tool not granted" in requests[1]["messages"][4]["content"]
    assert not (tmp_path / "bad").exists()
    assert events[0]["event"] == "started"
    assert events[-1]["event"] == "completed"
    assert not capsys.readouterr().out


def test_real_sse_stream_and_usage(server, tmp_path):
    url, requests, responses = server
    parts = [{"choices": [{"delta": {"content": "hello "}}]},
             {"choices": [{"delta": {"content": "world"}, "finish_reason": "stop"}]},
             {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}]
    body = "".join("data: " + json.dumps(item) + "\n\n" for item in parts) + "data: [DONE]\n\n"
    responses.append(("text/event-stream", body.encode()))
    events = []
    result = runtime(EndpointConfig(url, "small")).run(artifact(), cwd=tmp_path, on_event=events.append)
    assert result.stdout == "hello world"
    assert result.token_usage == 7
    assert "".join(e["text"] for e in events if e["event"] == "text_delta") == "hello world"
    assert "tools" not in requests[0]


def test_real_prosaic_inspection_and_cli(server, tmp_path):
    if not shutil.which("prosaic"):
        pytest.skip("Prosaic CLI is required for end-to-end conformance")
    url, requests, responses = server
    source = tmp_path / ".prosaic"
    (source / "subagents").mkdir(parents=True)
    (source / "subagents" / "summarizer.md").write_text(
        "---\nname: summarizer\ndescription: Summarize input\nexecution: agent\nmodel_tier: fast\neffort: low\n---\nSummarize {{args}}.\n")
    inspected = inspect_artifact("subagents/summarizer.md", source)
    assert inspected.frontmatter["model_tier"] == "fast"
    config = tmp_path / "runtime.toml"
    config.write_text(f'default_profile = "small"\n[routes]\nfast = "small"\n[profiles.small]\nbase_url = "{url}"\nmodel = "small"\n[profiles.small.features]\nstreaming = false\n')
    responses.append(completion())
    import sys
    completed = subprocess.run([sys.executable, "-m", "prosaic_runtime.cli", "subagents/summarizer.md",
        "--source", str(source), "--config", str(config), "--arguments", "findings"],
        capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["stdout"] == "done"
    assert "Summarize findings." in requests[0]["messages"][0]["content"]


@pytest.mark.parametrize("streaming", [False, True])
def test_response_budget_enforced(server, streaming):
    url, _, responses = server
    if streaming:
        responses.append(("text/event-stream", b'data: ' + b'x' * 300 + b'\n\n'))
    else:
        responses.append(completion("x" * 300))
    result = runtime(EndpointConfig(url, "small", max_response_bytes=100,
                                   features={"streaming": streaming})).run(artifact())
    assert result.exit_code != 0
    assert result.metadata["failure_reason"] == "budget_exceeded"


def test_missing_finish_is_not_success(server):
    url, _, responses = server
    responses.append(("text/event-stream", b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'))
    result = runtime(EndpointConfig(url, "small")).run(artifact())
    assert result.exit_code != 0
    assert result.metadata["failure_reason"] == "incomplete_response"


def test_cancel_after_stream_delta(server):
    url, _, responses = server
    responses.append(("text/event-stream", b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\ndata: [DONE]\n\n'))
    events = []
    result = runtime(EndpointConfig(url, "small")).run(artifact(), on_event=events.append,
        cancelled=lambda: any(e["event"] == "text_delta" for e in events))
    assert result.exit_code == 130


def test_public_tool_round_budget(server, tmp_path):
    url, _, responses = server
    (tmp_path / "input").write_text("content")
    turn = completion("", [{"id": "r", "type": "function", "function": {
        "name": "read_file", "arguments": '{"path":"input"}'}}])
    responses.extend([turn, turn])
    result = runtime(EndpointConfig(url, "small", features={"streaming": False})).run(
        artifact("read"), cwd=tmp_path,
        policy=RunPolicy(allowed_tools=frozenset({"read_file"}), read_roots=(".",), max_tool_rounds=1))
    assert result.exit_code != 0
    assert result.metadata["provider_error_code"] == "tool_round_limit"
