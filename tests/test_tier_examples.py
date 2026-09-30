"""Exercise the demo through real Prosaic inspection and a local HTTP endpoint."""
import json
from pathlib import Path
import subprocess
import sys

import yaml
from test_runtime import server, completion


SCRIPT = Path(__file__).resolve().parents[1] / "examples/run_tiers.py"


def config_file(tmp_path, url):
    path = tmp_path / "tiers.yml"
    path.write_text(yaml.safe_dump({
        "default_profile": "balanced", "allowed_tools": ["read_file"],
        "routes": {tier: tier for tier in ("fast", "balanced", "strong", "ultra")},
        "profiles": {tier: {"base_url": url, "model": f"model-{tier}",
                            "features": {"streaming": False}}
                     for tier in ("fast", "balanced", "strong", "ultra")},
    }))
    return path


def read_response(path):
    return completion("", [{"id": "read", "type": "function", "function": {
        "name": "read_file", "arguments": json.dumps({"path": path})}}])


def test_tiers_route_from_markdown_and_scope_tools(server, tmp_path):
    url, requests, responses = server
    config = config_file(tmp_path, url)
    responses.extend([
        completion("brief"),
        read_response("evidence/launch/metrics.md"), completion("analysis"),
        read_response("evidence/launch/field-notes.md"), completion("contradictions"),
        completion("decision"),
    ])
    run = subprocess.run([sys.executable, str(SCRIPT), "--config", str(config), "--events"],
                         cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr + run.stdout
    rows = [json.loads(line) for line in run.stdout.splitlines()]
    results = [row for row in rows if row["event"] == "result"]
    assert [row["tier"] for row in results] == ["fast", "balanced", "strong", "ultra"]
    assert [r["model"] for r in requests] == ["model-fast", "model-balanced", "model-balanced",
                                              "model-strong", "model-strong", "model-ultra"]
    assert "tools" not in requests[0] and "tools" not in requests[-1]
    assert [t["function"]["name"] for t in requests[1]["tools"]] == ["read_file"]
    assert any("M01" in m.get("content", "") for m in requests[2]["messages"] if m["role"] == "tool")
    assert len(requests[-1]["messages"][0]["content"].split()) >= 4500
    assert [r["tool_execution"] for r in results] == [False, True, True, False]
    assert all("elapsed_s" in r and "token_usage" in r for r in results)


def test_single_tier_text_output(server, tmp_path):
    url, requests, responses = server
    responses.append(completion("Executive decision"))
    run = subprocess.run([sys.executable, str(SCRIPT), "--config", str(config_file(tmp_path, url)),
                          "--tier", "ultra"], cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert run.returncode == 0, run.stderr
    assert len(requests) == 1 and requests[0]["model"] == "model-ultra"
    assert "Executive decision" in run.stdout and "model-ultra" in run.stdout


def test_tool_demo_fails_if_model_never_reads(server, tmp_path):
    url, requests, responses = server
    responses.append(completion("Unsupported confident answer"))
    run = subprocess.run([sys.executable, str(SCRIPT), "--config", str(config_file(tmp_path, url)),
                          "--tier", "balanced", "--events"],
                         cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert run.returncode == 1
    row = json.loads(run.stdout.splitlines()[-1])
    assert row["demo_ok"] is False and row["tool_execution"] is False


def test_failure_stops_before_next_tier(server, tmp_path):
    url, requests, responses = server
    content_type, body = completion("unfinished")
    reply = json.loads(body)
    reply["choices"][0]["finish_reason"] = "length"
    responses.append((content_type, json.dumps(reply).encode()))
    run = subprocess.run([sys.executable, str(SCRIPT), "--config", str(config_file(tmp_path, url))],
                         cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert run.returncode == 1 and len(requests) == 1


def test_invalid_timeout_rejected_before_inference(server, tmp_path):
    url, requests, responses = server
    run = subprocess.run([sys.executable, str(SCRIPT), "--config", str(config_file(tmp_path, url)),
                          "--timeout", "-1"], capture_output=True, text=True, timeout=15)
    assert run.returncode == 2 and not requests
    assert "timeout" in run.stderr
