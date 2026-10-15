import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from prosaic_runtime import EndpointConfig, ProsaicArtifact, ProsaicRuntime, RuntimeConfig
from prosaic_runtime.accounting import MemoryRecorder


@pytest.mark.parametrize("provider", ["openai-compatible", "anthropic"])
@pytest.mark.parametrize("accounted", [True, False])
def test_http_correlation_matches_durable_intent_and_is_unique(accounted, provider):
    recorder = MemoryRecorder(provider_id=provider)
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            call = self.headers.get("X-Correlation-ID")
            assert call and len(call) == 32
            if accounted:
                assert call in recorder.intents  # Intent is durable before network dispatch.
            assert self.headers.get("X-Client-Request-Id") == call
            assert self.headers.get("Idempotency-Key") is None
            seen.append(call)
            value = (
                {
                    "id": "provider-response",
                    "model": "fixture",
                    "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
                }
            )
            if provider == "anthropic":
                value = {"id": "provider-response", "type": "message", "role": "assistant",
                    "model": "fixture", "content": [{"type": "text", "text": "ok"}],
                    "stop_reason": "end_turn", "usage": {"input_tokens": 2, "output_tokens": 1}}
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = EndpointConfig(
            f"http://127.0.0.1:{server.server_port}/v1", "fixture", features={"streaming": False}, provider=provider
        )
        runtime = ProsaicRuntime(
            RuntimeConfig({"p": endpoint}, {"fast": "p"}, "p", frozenset()),
            accounting=recorder if accounted else None,
        )
        artifact = ProsaicArtifact.from_inspection(
            {
                "id": "subagents/fixture.md",
                "type": "subagent",
                "frontmatter": {"model_tier": "fast", "tools": []},
                "body": "Return ok.",
            }
        )
        for _ in range(2):
            result = runtime.run(artifact)
            assert result.exit_code == 0
            if accounted:
                assert result.metadata["accounting_v1"]["provider_call_ids"] == [seen[-1]]
            else:
                assert "http_correlation_ids" not in result.metadata
        assert len(set(seen)) == 2
        if accounted:
            assert set(seen) == set(recorder.observations)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
