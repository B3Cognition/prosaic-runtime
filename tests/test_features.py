import json
from pathlib import Path
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from test_runtime import server, completion, artifact, runtime
from prosaic_runtime import EndpointConfig, RuntimeConfig, RunPolicy


def config_file(tmp_path, url):
    path = tmp_path / "runtime.yaml"
    path.write_text(f'''default_profile: small
allowed_tools: [read_file]
limits:
  timeout_s: 180
  max_tool_rounds: 4
routes:
  fast: small
profiles:
  small:
    base_url: {url}
    model: test
    features:
      streaming: false
''')
    return path


def cli(*args):
    return subprocess.run([sys.executable, "-m", "prosaic_runtime.cli", *map(str, args)],
                          text=True, capture_output=True, timeout=20)


def test_configured_limits_and_validation(tmp_path):
    path = config_file(tmp_path, "http://localhost:9/v1")
    config = RuntimeConfig.load(path)
    assert config.limits.timeout_s == 180
    assert config.limits.max_tool_rounds == 4
    path.write_text(path.read_text().replace("timeout_s: 180", "timeout_s: -1"))
    with pytest.raises(ValueError):
        RuntimeConfig.load(path)


def test_text_output_and_stderr_progress(server, tmp_path):
    url, requests, responses = server
    responses.append(completion("plain answer"))
    config = config_file(tmp_path, url)
    source = Path(__file__).resolve().parents[1] / "examples/.prosaic"
    result = cli("subagents/summarizer.md", "--config", config, "--source", source,
                 "--output", "text", "--arguments", "test")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "plain answer\n"
    assert "started" in result.stderr
    assert "completed" in result.stderr
    assert len(requests) == 1


def test_structured_tool_events_cover_success_and_denial(server, tmp_path):
    url, requests, responses = server
    (tmp_path / "input").write_text("evidence")
    responses.extend([completion("", [
        {"id": "a", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"input"}'}},
        {"id": "b", "type": "function", "function": {"name": "write_file", "arguments": '{"path":"bad","content":"x"}'}},
    ]), completion()])
    events = []
    result = runtime(EndpointConfig(url, "test", features={"streaming": False})).run(
        artifact("write"), cwd=tmp_path, on_event=events.append,
        policy=RunPolicy(allowed_tools=frozenset({"read_file"}), read_roots=(".",)))
    assert result.exit_code == 0
    starts = [e for e in events if e["event"] == "tool_started"]
    ends = [e for e in events if e["event"] == "tool_completed"]
    assert [e["name"] for e in starts] == ["read_file", "write_file"]
    assert [e["status"] for e in ends] == ["ok", "error"]
    assert [e["call_id"] for e in ends] == ["a", "b"]
    assert all(e["duration_ms"] >= 0 for e in ends)
    assert all("arguments" not in e and "content" not in e for e in ends)


def test_smoke_requires_explicit_live_opt_in(tmp_path):
    result = cli("smoke", "--config", config_file(tmp_path, "http://localhost:9/v1"))
    assert result.returncode == 2
    assert "--live" in result.stderr


def test_conformance_without_live_is_explicitly_unexecuted(server, tmp_path):
    url, requests, responses = server
    result = cli('conformance', '--config', config_file(tmp_path, url), '--profile', 'small', '--quiet')
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report['qualification'] == 'not_qualified'
    assert all(c['state'] == 'not_run' for c in report['cases'])
    assert requests == []


def test_conformance_default_does_not_contact_unreachable_url(tmp_path):
    result = cli('conformance', '--config', config_file(tmp_path, 'http://localhost:9/v1'), '--quiet')
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['qualification'] == 'not_qualified'


def test_doctor_checks_without_inference(server, tmp_path):
    url, requests, responses = server
    # This fixture does not implement GET: discovery must be reported, never replaced by inference.
    result = cli("doctor", "--config", config_file(tmp_path, url))
    report = json.loads(result.stdout)
    assert report["command"] == "doctor"
    assert report["checks"]["prosaic"]["status"] == "ok"
    assert report["checks"]["discovery"]["status"] == "warning"
    assert report["checks"]["inference"]["status"] == "skipped"
    assert requests == []


def test_smoke_does_not_claim_streaming_or_tools_without_observing_them(server, tmp_path):
    url, requests, responses = server
    responses.extend([completion("done"), completion("I read it")])
    result = cli("smoke", "--live", "--config", config_file(tmp_path, url), "--quiet")
    report = json.loads(result.stdout)
    assert result.returncode == 1
    assert report["tests"]["without_tools"]["completion"] is True
    assert report["tests"]["with_tools"]["tool_execution"] is False
    assert report["tests"]["without_tools"]["streaming"] is False


def test_cli_limits_override_yaml(server, tmp_path):
    url, requests, responses = server
    config = config_file(tmp_path, url)
    source = Path(__file__).resolve().parents[1] / "examples/.prosaic"
    result = cli("subagents/summarizer.md", "--config", config, "--source", source, "--timeout", "-1")
    assert result.returncode == 2
    assert requests == []


def test_cli_round_override_stops_tool_loop(server, tmp_path):
    url, requests, responses = server
    (tmp_path / "input").write_text("evidence")
    turn = completion("", [{"id": "read", "type": "function", "function": {
        "name": "read_file", "arguments": '{"path":"input"}'}}])
    responses.extend([turn, turn])
    config = config_file(tmp_path, url)
    source = Path(__file__).resolve().parents[1] / "examples/.prosaic"
    result = cli("subagents/reviewer.md", "--config", config, "--source", source,
                 "--cwd", tmp_path, "--allow-tool", "read_file", "--read-root", ".",
                 "--max-tool-rounds", "1", "--quiet")
    assert result.returncode == 1
    assert json.loads(result.stdout)["metadata"]["provider_error_code"] == "tool_round_limit"
    assert len(requests) == 2


def test_smoke_success_with_real_tool_roundtrip(server, tmp_path):
    url, requests, responses = server
    responses.extend([completion("summary"), completion("", [{"id": "read", "type": "function", "function": {
        "name": "read_file", "arguments": '{"path":"evidence/pilot.md"}'}}]), completion("review")])
    result = cli("smoke", "--live", "--config", config_file(tmp_path, url), "--quiet")
    report = json.loads(result.stdout)
    assert result.returncode == 0, result.stderr
    assert report["ok"]
    assert report["tests"]["with_tools"]["tool_execution"]
    assert any("120 requests" in m.get("content", "") for m in requests[2]["messages"] if m["role"] == "tool")


def test_doctor_inference_is_explicit(server, tmp_path):
    url, requests, responses = server
    responses.append(completion("summary"))
    result = cli("doctor", "--inference", "--config", config_file(tmp_path, url), "--quiet")
    report = json.loads(result.stdout)
    assert result.returncode == 0, result.stderr
    assert report["checks"]["inference"]["completion"]
    assert len(requests) == 1


def test_text_and_events_are_mutually_exclusive():
    result = cli("subagents/test.md", "--events", "--output", "text")
    assert result.returncode == 2
    assert "not allowed" in result.stderr


@pytest.mark.parametrize("status,expected", [(200, "ok"), (302, "failed"), (401, "failed"), (403, "failed"), (404, "warning"), (429, "failed")])
def test_doctor_discovery_auth_and_redaction(tmp_path, monkeypatch, status, expected):
    from prosaic_runtime.diagnostics import doctor
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            self.send_response(status)
            if status == 302:
                self.send_header("Location", "/redirected")
            self.end_headers()
            self.wfile.write(json.dumps({"data": [{"id": "test"}], "error": "secret-test-token"}).encode())
    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        from dataclasses import replace
        config = RuntimeConfig.load(config_file(tmp_path, f"http://127.0.0.1:{http.server_port}/v1"))
        config = replace(config, profiles={"small": replace(config.profiles["small"], api_key_env="TEST_DOCTOR_KEY")})
        monkeypatch.setenv("TEST_DOCTOR_KEY", "secret-test-token")
        report = doctor(config, "small")
        assert report["checks"]["discovery"]["status"] == expected
        assert "secret-test-token" not in json.dumps(report)
        assert requests == [("/v1/models", "Bearer secret-test-token")]
    finally:
        http.shutdown()
        thread.join(timeout=2)
        http.server_close()


@pytest.mark.parametrize('start_delay',[0,0.08])
def test_progress_heartbeat_stops_and_stays_off_stdout(capsys,monkeypatch,start_delay):
    from prosaic_runtime.console import Progress
    import time
    import threading
    original_thread=threading.Thread
    def delayed_thread(*args,**kwargs):
        target=kwargs['target']
        kwargs['target']=lambda:(time.sleep(start_delay),target())
        return original_thread(*args,**kwargs)
    monkeypatch.setattr(threading,'Thread',delayed_thread)
    observed=threading.Event()
    class ObservedProgress(Progress):
        def write(self,text):
            super().write(text)
            observed.set()
    with ObservedProgress(interval=0.01) as progress:
        assert observed.wait(5), 'heartbeat did not reach stderr'
    assert not progress.thread.is_alive()
    assert progress.stop.is_set()
    out = capsys.readouterr()
    assert out.out == ""
    assert "working" in out.err
    assert capsys.readouterr().err == ""


def test_api_uses_yaml_limits_when_policy_omitted(server, tmp_path):
    from prosaic_runtime import ProsaicRuntime
    url, requests, responses = server
    config = config_file(tmp_path, url)
    config.write_text(config.read_text().replace("timeout_s: 180", "timeout_s: 0.000000001"))
    result = ProsaicRuntime.from_config(config).run(artifact())
    assert result.timed_out
    assert requests == []
