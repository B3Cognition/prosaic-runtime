import json
from pathlib import Path
import subprocess
import sys

import pytest
from test_runtime import server, completion


@pytest.mark.parametrize("events", [False, True])
def test_sample_program_runs_both_examples_outside_checkout(server, tmp_path, events):
    url, requests, responses = server
    config = tmp_path / "local.yml"
    config.write_text('''default_profile: small
allowed_tools: [read_file]
routes:
  fast: small
profiles:
  small:
    base_url: http://localhost:9/v1
    model: placeholder
    features:
      streaming: false
''')
    responses.extend([completion("summary"), completion("", [{"id": "read", "type": "function", "function": {
        "name": "read_file", "arguments": '{"path":"evidence/pilot.md"}'}}]), completion("review")])
    script = Path(__file__).resolve().parents[1] / "examples/run_examples.py"
    args = [sys.executable, str(script), "--config", str(config), "--base-url", url, "--model", "test-model"]
    if events:
        args.append("--events")
    result = subprocess.run(args, cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    output = [json.loads(line) for line in result.stdout.splitlines()]
    results = [item for item in output if item["event"] == "result"]
    assert [item["example"] for item in results] == ["summarizer", "reviewer"]
    assert [item["stdout"] for item in results] == ["summary", "review"]
    assert "tools" not in requests[0]
    assert all(request["model"] == "test-model" for request in requests)
    assert [tool["function"]["name"] for tool in requests[1]["tools"]] == ["read_file"]
    assert any("120 requests" in m.get("content", "") for m in requests[2]["messages"] if m["role"] == "tool")
    if events:
        assert any(item["event"] == "tool_completed" and item["status"] == "ok" for item in output)
    else:
        assert len(output) == 2


def test_sample_program_rejects_invalid_timeout_before_network(tmp_path):
    script = Path(__file__).resolve().parents[1] / "examples/run_examples.py"
    result = subprocess.run([sys.executable, str(script), "--timeout", "-1"],
                            cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert "timeout" in result.stderr
