"""Transport conformance tests extracted from Echelon (MIT); see NOTICE.md."""
from dataclasses import replace
import io
import json
import socket
import urllib.error
from pathlib import Path
import pytest
from prosaic_runtime.types import Invocation as CliRunRequest
from prosaic_runtime.config import EndpointConfig

def _openai_config(features=None):
    return EndpointConfig(base_url="http://127.0.0.1:8000/v1", model="local-model",
                          api_key_env="LOCAL_LLM_API_KEY", temperature=0.2,
                          max_tokens=256, features=features or {})

def test_openai_compatible_backend_posts_chat_completion(tmp_path, monkeypatch) -> None:
    from prosaic_runtime.openai_compatible import OpenAICompatibleBackend

    captured = {}
    monkeypatch.setenv("LOCAL_LLM_API_KEY", "secret-token")

    class FakeResponse:
        status = 200
        headers = {
            "Content-Type": "application/json",
            "X-Request-ID": "req_123",
            "Set-Cookie": "session=secret",
        }

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({
                "choices": [
                    {"message": {"content": "echelon_result:\n  verdict: DONE\n"}}
                ],
                "usage": {"prompt_tokens": 7, "completion_tokens": 5},
            }).encode()

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.header_items())
        captured["payload"] = json.loads(request.data.decode())
        return FakeResponse()

    monkeypatch.setattr(
        "prosaic_runtime.openai_compatible.urllib.request.urlopen",
        fake_urlopen,
    )
    backend = OpenAICompatibleBackend(_openai_config(features={"streaming": False}))
    result = backend.run_prompt(
        CliRunRequest(
            cwd=str(tmp_path),
            prompt="Return a result.",
            env={},
            timeout_s=12.5,
        )
    )

    assert result.exit_code == 0
    assert result.stdout == "echelon_result:\n  verdict: DONE\n"
    assert result.token_usage == 12
    assert result.metadata["token_usage_details"] == {
        "prompt_tokens": 7,
        "completion_tokens": 5,
        "total_tokens": 12,
    }
    assert result.metadata["http_status"] == 200
    assert result.metadata["request_model"] == "local-model"
    assert result.metadata["raw_response_headers"] == {
        "content-type": "application/json",
        "x-request-id": "req_123",
    }
    assert captured["url"] == "http://127.0.0.1:8000/v1/chat/completions"
    assert captured["timeout"] == 12.5
    assert captured["headers"]["Authorization"] == "Bearer secret-token"
    assert captured["payload"]["model"] == "local-model"
    assert captured["payload"]["messages"] == [
        {"role": "user", "content": "Return a result."}
    ]
    assert captured["payload"]["temperature"] == 0.2
    assert captured["payload"]["max_tokens"] == 256
    assert "stream" not in captured["payload"]


def test_openai_compatible_backend_can_request_json_mode(
    tmp_path, monkeypatch
) -> None:
    from prosaic_runtime.openai_compatible import OpenAICompatibleBackend

    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "{\"ok\": true}"}}],
            }).encode()

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data.decode())
        return FakeResponse()

    monkeypatch.setattr(
        "prosaic_runtime.openai_compatible.urllib.request.urlopen",
        fake_urlopen,
    )

    result = OpenAICompatibleBackend(
        _openai_config(features={"streaming": False, "json_mode": True})
    ).run_prompt(
        CliRunRequest(
            cwd=str(tmp_path),
            prompt="Return JSON.",
            env={},
            timeout_s=12.5,
        )
    )

    assert result.exit_code == 0
    assert captured["payload"]["response_format"] == {"type": "json_object"}


def test_openai_compatible_backend_can_request_reasoning_effort(
    tmp_path, monkeypatch
) -> None:
    from prosaic_runtime.openai_compatible import OpenAICompatibleBackend

    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "ok"}}],
            }).encode()

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data.decode())
        return FakeResponse()

    monkeypatch.setattr(
        "prosaic_runtime.openai_compatible.urllib.request.urlopen",
        fake_urlopen,
    )

    result = OpenAICompatibleBackend(
        _openai_config(features={"streaming": False, "reasoning_effort": "high"})
    ).run_prompt(
        CliRunRequest(
            cwd=str(tmp_path),
            prompt="Return a result.",
            env={},
            timeout_s=12.5,
        )
    )

    assert result.exit_code == 0
    assert captured["payload"]["reasoning_effort"] == "high"


def test_openai_compatible_backend_uses_prompt_metadata_overrides(
    tmp_path, monkeypatch
) -> None:
    from prosaic_runtime.openai_compatible import OpenAICompatibleBackend

    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "ok"}}],
            }).encode()

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data.decode())
        return FakeResponse()

    monkeypatch.setattr(
        "prosaic_runtime.openai_compatible.urllib.request.urlopen",
        fake_urlopen,
    )

    result = OpenAICompatibleBackend(
        _openai_config(features={"streaming": False, "reasoning_effort": "low"})
    ).run_prompt(
        CliRunRequest(
            cwd=str(tmp_path),
            prompt="Return a result.",
            env={},
            timeout_s=12.5,
            metadata={
                "prompt_metadata": {
                    "model": "frontmatter-model",
                    "effort": "high",
                    "temperature": 0.4,
                    "max_tokens": 1024,
                }
            },
        )
    )

    assert result.exit_code == 0
    assert captured["payload"]["model"] == "frontmatter-model"
    assert captured["payload"]["reasoning_effort"] == "high"
    assert captured["payload"]["temperature"] == 0.4
    assert captured["payload"]["max_tokens"] == 1024


def test_openai_compatible_backend_records_http_error_response_headers(
    tmp_path, monkeypatch
) -> None:
    from prosaic_runtime.openai_compatible import OpenAICompatibleBackend

    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            429,
            "Too Many Requests",
            {
                "Content-Type": "application/json",
                "OpenAI-Request-ID": "req_error",
                "Set-Cookie": "session=secret",
            },
            io.BytesIO(b'{"error":"rate limit"}'),
        )

    monkeypatch.setattr(
        "prosaic_runtime.openai_compatible.urllib.request.urlopen",
        fake_urlopen,
    )

    result = OpenAICompatibleBackend(
        _openai_config(features={"streaming": False})
    ).run_prompt(
        CliRunRequest(
            cwd=str(tmp_path),
            prompt="Return a result.",
            env={},
            timeout_s=12.5,
        )
    )

    assert result.exit_code == 429
    assert result.metadata["provider_error_code"] == "http_error"
    assert result.metadata["http_status"] == 429
    assert result.metadata["raw_response_headers"] == {
        "content-type": "application/json",
        "openai-request-id": "req_error",
    }


def test_openai_compatible_backend_reads_nonstreaming_content_blocks(
    tmp_path, monkeypatch
) -> None:
    from prosaic_runtime.openai_compatible import OpenAICompatibleBackend

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "text", "text": "echelon_result:\n"},
                                {"type": "text", "text": "  verdict: PASS\n"},
                            ],
                        },
                    }
                ],
                "usage": {"total_tokens": 11},
            }).encode()

    monkeypatch.setattr(
        "prosaic_runtime.openai_compatible.urllib.request.urlopen",
        lambda request, timeout: FakeResponse(),
    )

    result = OpenAICompatibleBackend(_openai_config(features={"streaming": False})).run_prompt(
        CliRunRequest(
            cwd=str(tmp_path),
            prompt="Return a result.",
            env={},
            timeout_s=12.5,
        )
    )

    assert result.exit_code == 0
    assert result.stdout == "echelon_result:\n  verdict: PASS\n"
    assert result.token_usage == 11


def test_openai_compatible_backend_records_nonstreaming_response_metadata(
    tmp_path, monkeypatch
) -> None:
    from prosaic_runtime.openai_compatible import OpenAICompatibleBackend

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({
                "id": "chatcmpl-123",
                "object": "chat.completion",
                "created": 1_725_000_000,
                "model": "local-model",
                "system_fingerprint": "fp_local",
                "choices": [{"message": {"content": "ok"}}],
            }).encode()

    monkeypatch.setattr(
        "prosaic_runtime.openai_compatible.urllib.request.urlopen",
        lambda request, timeout: FakeResponse(),
    )

    result = OpenAICompatibleBackend(_openai_config(features={"streaming": False})).run_prompt(
        CliRunRequest(
            cwd=str(tmp_path),
            prompt="Return a result.",
            env={},
            timeout_s=12.5,
        )
    )

    assert result.exit_code == 0
    assert result.metadata["request_model"] == "local-model"
    assert result.metadata["raw_response_metadata"] == {
        "id": "chatcmpl-123",
        "object": "chat.completion",
        "created": 1_725_000_000,
        "model": "local-model",
        "system_fingerprint": "fp_local",
    }


def test_openai_compatible_backend_rejects_nonstreaming_truncation(
    tmp_path, monkeypatch
) -> None:
    from prosaic_runtime.openai_compatible import OpenAICompatibleBackend

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({
                "choices": [
                    {
                        "message": {"content": "echelon_result:\n  verdict:"},
                        "finish_reason": "length",
                    }
                ],
                "usage": {"total_tokens": 31},
            }).encode()

    monkeypatch.setattr(
        "prosaic_runtime.openai_compatible.urllib.request.urlopen",
        lambda request, timeout: FakeResponse(),
    )

    result = OpenAICompatibleBackend(_openai_config(features={"streaming": False})).run_prompt(
        CliRunRequest(
            cwd=str(tmp_path),
            prompt="Return a result.",
            env={},
            timeout_s=12.5,
        )
    )

    assert result.exit_code == 1
    assert result.stdout == "echelon_result:\n  verdict:"
    assert result.token_usage == 31
    assert "finish_reason=length" in result.stderr
    assert result.metadata["finish_reason"] == "length"
    assert result.metadata["provider_error_code"] == "incomplete_generation"


def test_openai_compatible_backend_prefers_request_env_api_key(
    tmp_path, monkeypatch
) -> None:
    from prosaic_runtime.openai_compatible import OpenAICompatibleBackend

    captured = {}
    monkeypatch.setenv("LOCAL_LLM_API_KEY", "process-token")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "ok"}}],
            }).encode()

    def fake_urlopen(request, timeout):
        captured["headers"] = dict(request.header_items())
        return FakeResponse()

    monkeypatch.setattr(
        "prosaic_runtime.openai_compatible.urllib.request.urlopen",
        fake_urlopen,
    )

    result = OpenAICompatibleBackend(_openai_config(features={"streaming": False})).run_prompt(
        CliRunRequest(
            cwd=str(tmp_path),
            prompt="Return a result.",
            env={"LOCAL_LLM_API_KEY": "request-token"},
            timeout_s=12.5,
        )
    )

    assert result.exit_code == 0
    assert captured["headers"]["Authorization"] == "Bearer request-token"


def test_openai_compatible_backend_reads_api_key_file(tmp_path, monkeypatch) -> None:
    from prosaic_runtime.openai_compatible import OpenAICompatibleBackend

    captured = {}
    token_file = tmp_path / ".omlx_token"
    token_file.write_text("file-token\n", encoding="utf-8")
    config = _openai_config(features={"streaming": False})
    config = replace(config, api_key_env=None, api_key_file=str(token_file))

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "ok"}}],
            }).encode()

    def fake_urlopen(request, timeout):
        captured["headers"] = dict(request.header_items())
        return FakeResponse()

    monkeypatch.setattr(
        "prosaic_runtime.openai_compatible.urllib.request.urlopen",
        fake_urlopen,
    )

    result = OpenAICompatibleBackend(config).run_prompt(
        CliRunRequest(
            cwd=str(tmp_path),
            prompt="Return a result.",
            env={},
            timeout_s=12.5,
        )
    )

    assert result.exit_code == 0
    assert captured["headers"]["Authorization"] == "Bearer file-token"


def test_openai_compatible_backend_rejects_missing_api_key_file(
    tmp_path, monkeypatch
) -> None:
    from prosaic_runtime.openai_compatible import OpenAICompatibleBackend

    config = _openai_config(features={"streaming": False})
    config = replace(config, api_key_env=None, api_key_file=str(tmp_path / "missing-token"))

    def fail_urlopen(request, timeout):
        raise AssertionError("request should be blocked before HTTP")

    monkeypatch.setattr(
        "prosaic_runtime.openai_compatible.urllib.request.urlopen",
        fail_urlopen,
    )

    result = OpenAICompatibleBackend(config).run_prompt(
        CliRunRequest(
            cwd=str(tmp_path),
            prompt="Return a result.",
            env={},
            timeout_s=12.5,
        )
    )

    assert result.exit_code == 1
    assert "API key file" in result.stderr
    assert "missing-token" in result.stderr
    assert result.metadata["provider_error_code"] == "api_key_file_error"


def test_openai_compatible_backend_rejects_empty_api_key_file(
    tmp_path, monkeypatch
) -> None:
    from prosaic_runtime.openai_compatible import OpenAICompatibleBackend

    token_file = tmp_path / ".omlx_token"
    token_file.write_text("\n", encoding="utf-8")
    config = _openai_config(features={"streaming": False})
    config = replace(config, api_key_env=None, api_key_file=str(token_file))

    def fail_urlopen(request, timeout):
        raise AssertionError("request should be blocked before HTTP")

    monkeypatch.setattr(
        "prosaic_runtime.openai_compatible.urllib.request.urlopen",
        fail_urlopen,
    )

    result = OpenAICompatibleBackend(config).run_prompt(
        CliRunRequest(
            cwd=str(tmp_path),
            prompt="Return a result.",
            env={},
            timeout_s=12.5,
        )
    )

    assert result.exit_code == 1
    assert "API key file is empty" in result.stderr
    assert result.metadata["provider_error_code"] == "api_key_file_error"


def test_openai_compatible_backend_streams_sse_and_excludes_reasoning(
    tmp_path, monkeypatch
) -> None:
    from prosaic_runtime.openai_compatible import OpenAICompatibleBackend

    captured = {}

    class FakeResponse:
        status = 200
        headers = {
            "Content-Type": "text/event-stream",
            "X-Request-ID": "req_stream",
        }

        def __init__(self) -> None:
            self._lines = iter([
                b'data: {"choices":[{"delta":{"reasoning_content":"private notes"}}]}\n',
                b'data: {"choices":[{"delta":{"content":"echelon_result:\\n"}}]}\n',
                b'data: {"choices":[{"delta":{"content":"  verdict: PASS\\n"},"finish_reason":"stop"}],"usage":{"total_tokens":42}}\n',
                b"data: [DONE]\n",
            ])

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def readline(self):
            return next(self._lines, b"")

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data.decode())
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(
        "prosaic_runtime.openai_compatible.urllib.request.urlopen",
        fake_urlopen,
    )
    backend = OpenAICompatibleBackend(_openai_config())
    result = backend.run_prompt(
        CliRunRequest(
            cwd=str(tmp_path),
            prompt="Return a result.",
            env={},
            timeout_s=12.5,
        )
    )

    assert result.exit_code == 0
    assert result.stdout == "echelon_result:\n  verdict: PASS\n"
    assert "private notes" not in result.stdout
    assert result.token_usage == 42
    assert result.metadata["token_usage_details"] == {"total_tokens": 42}
    assert captured["payload"]["stream"] is True
    assert captured["payload"]["stream_options"] == {"include_usage": True}
    assert captured["timeout"] == 12.5
    assert result.metadata["http_status"] == 200
    assert result.metadata["raw_response_headers"] == {
        "content-type": "text/event-stream",
        "x-request-id": "req_stream",
    }
    assert result.metadata["streamed"] is True
    assert result.metadata["finish_reason"] == "stop"
    assert result.metadata["reasoning_content_observed"] is True


def test_openai_compatible_backend_can_omit_stream_options(
    tmp_path, monkeypatch
) -> None:
    from prosaic_runtime.openai_compatible import OpenAICompatibleBackend

    captured = {}

    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __init__(self) -> None:
            self._lines = iter([
                b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}],"usage":{"total_tokens":1}}\n',
                b"data: [DONE]\n",
            ])

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def readline(self):
            return next(self._lines, b"")

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data.decode())
        return FakeResponse()

    monkeypatch.setattr(
        "prosaic_runtime.openai_compatible.urllib.request.urlopen",
        fake_urlopen,
    )

    result = OpenAICompatibleBackend(
        _openai_config(features={"stream_options": False})
    ).run_prompt(
        CliRunRequest(
            cwd=str(tmp_path),
            prompt="Return a result.",
            env={},
            timeout_s=12.5,
        )
    )

    assert result.exit_code == 0
    assert captured["payload"]["stream"] is True
    assert "stream_options" not in captured["payload"]


def test_openai_compatible_backend_reports_reasoning_content_policy(
    tmp_path, monkeypatch
) -> None:
    from prosaic_runtime.openai_compatible import OpenAICompatibleBackend

    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __init__(self) -> None:
            self._lines = iter([
                b'data: {"choices":[{"delta":{"content":"echelon_result:\\n"},"finish_reason":"stop"}]}\n',
                b"data: [DONE]\n",
            ])

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def readline(self):
            return next(self._lines, b"")

    monkeypatch.setattr(
        "prosaic_runtime.openai_compatible.urllib.request.urlopen",
        lambda request, timeout: FakeResponse(),
    )

    result = OpenAICompatibleBackend(
        _openai_config(features={"reasoning_content": "merged"})
    ).run_prompt(
        CliRunRequest(
            cwd=str(tmp_path),
            prompt="Return a result.",
            env={},
            timeout_s=12.5,
        )
    )

    assert result.exit_code == 0
    assert result.metadata["reasoning_content_policy"] == "merged"
    assert result.metadata["reasoning_content_observed"] is False


def test_openai_compatible_backend_streams_content_blocks(
    tmp_path, monkeypatch
) -> None:
    from prosaic_runtime.openai_compatible import OpenAICompatibleBackend

    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __init__(self) -> None:
            self._lines = iter([
                b'data: {"choices":[{"delta":{"content":[{"type":"text","text":"echelon_result:\\n"}]}}]}\n',
                b'data: {"choices":[{"delta":{"content":[{"type":"text","text":"  verdict: PASS\\n"}]},"finish_reason":"stop"}],"usage":{"total_tokens":13}}\n',
                b"data: [DONE]\n",
            ])

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def readline(self):
            return next(self._lines, b"")

    monkeypatch.setattr(
        "prosaic_runtime.openai_compatible.urllib.request.urlopen",
        lambda request, timeout: FakeResponse(),
    )

    result = OpenAICompatibleBackend(_openai_config()).run_prompt(
        CliRunRequest(
            cwd=str(tmp_path),
            prompt="Return a result.",
            env={},
            timeout_s=12.5,
        )
    )

    assert result.exit_code == 0
    assert result.stdout == "echelon_result:\n  verdict: PASS\n"
    assert result.token_usage == 13


def test_openai_compatible_backend_streams_message_content_chunks(
    tmp_path, monkeypatch
) -> None:
    from prosaic_runtime.openai_compatible import OpenAICompatibleBackend

    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __init__(self) -> None:
            self._lines = iter([
                b'data: {"choices":[{"message":{"content":"echelon_result:\\n"}}]}\n',
                b'data: {"choices":[{"message":{"content":"  verdict: PASS\\n"},"finish_reason":"stop"}],"usage":{"total_tokens":23}}\n',
                b"data: [DONE]\n",
            ])

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def readline(self):
            return next(self._lines, b"")

    monkeypatch.setattr(
        "prosaic_runtime.openai_compatible.urllib.request.urlopen",
        lambda request, timeout: FakeResponse(),
    )

    result = OpenAICompatibleBackend(_openai_config()).run_prompt(
        CliRunRequest(
            cwd=str(tmp_path),
            prompt="Return a result.",
            env={},
            timeout_s=12.5,
        )
    )

    assert result.exit_code == 0
    assert result.stdout == "echelon_result:\n  verdict: PASS\n"
    assert result.token_usage == 23


def test_openai_compatible_backend_streams_choice_text_chunks(
    tmp_path, monkeypatch
) -> None:
    from prosaic_runtime.openai_compatible import OpenAICompatibleBackend

    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __init__(self) -> None:
            self._lines = iter([
                b'data: {"choices":[{"text":"echelon_result:\\n"}]}\n',
                b'data: {"choices":[{"text":"  verdict: PASS\\n","finish_reason":"stop"}],"usage":{"total_tokens":29}}\n',
                b"data: [DONE]\n",
            ])

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def readline(self):
            return next(self._lines, b"")

    monkeypatch.setattr(
        "prosaic_runtime.openai_compatible.urllib.request.urlopen",
        lambda request, timeout: FakeResponse(),
    )

    result = OpenAICompatibleBackend(_openai_config()).run_prompt(
        CliRunRequest(
            cwd=str(tmp_path),
            prompt="Return a result.",
            env={},
            timeout_s=12.5,
        )
    )

    assert result.exit_code == 0
    assert result.stdout == "echelon_result:\n  verdict: PASS\n"
    assert result.token_usage == 29


def test_openai_compatible_backend_observes_streaming_reasoning_aliases(
    tmp_path, monkeypatch
) -> None:
    from prosaic_runtime.openai_compatible import OpenAICompatibleBackend

    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __init__(self) -> None:
            self._lines = iter([
                b'data: {"choices":[{"delta":{"reasoning":"private notes"}}]}\n',
                b'data: {"choices":[{"delta":{"reasoning_text":"more private notes"}}]}\n',
                b'data: {"choices":[{"delta":{"content":"echelon_result:\\n  verdict: PASS\\n"},"finish_reason":"stop"}],"usage":{"total_tokens":31}}\n',
                b"data: [DONE]\n",
            ])

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def readline(self):
            return next(self._lines, b"")

    monkeypatch.setattr(
        "prosaic_runtime.openai_compatible.urllib.request.urlopen",
        lambda request, timeout: FakeResponse(),
    )

    result = OpenAICompatibleBackend(_openai_config()).run_prompt(
        CliRunRequest(
            cwd=str(tmp_path),
            prompt="Return a result.",
            env={},
            timeout_s=12.5,
        )
    )

    assert result.exit_code == 0
    assert result.stdout == "echelon_result:\n  verdict: PASS\n"
    assert "private notes" not in result.stdout
    assert result.metadata["reasoning_content_observed"] is True


def test_openai_compatible_backend_ignores_empty_stream_data_events(
    tmp_path, monkeypatch
) -> None:
    from prosaic_runtime.openai_compatible import OpenAICompatibleBackend

    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __init__(self) -> None:
            self._lines = iter([
                b"data:\n",
                b"\n",
                b'data: {"choices":[{"delta":{"content":"echelon_result:\\n"}}]}\n',
                b'data: {"choices":[{"delta":{"content":"  verdict: PASS\\n"},"finish_reason":"stop"}],"usage":{"total_tokens":33}}\n',
                b"data: [DONE]\n",
            ])

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def readline(self):
            return next(self._lines, b"")

    monkeypatch.setattr(
        "prosaic_runtime.openai_compatible.urllib.request.urlopen",
        lambda request, timeout: FakeResponse(),
    )

    result = OpenAICompatibleBackend(_openai_config()).run_prompt(
        CliRunRequest(
            cwd=str(tmp_path),
            prompt="Return a result.",
            env={},
            timeout_s=12.5,
        )
    )

    assert result.exit_code == 0
    assert result.stdout == "echelon_result:\n  verdict: PASS\n"
    assert result.token_usage == 33


def test_openai_compatible_backend_accepts_indented_sse_data_lines(
    tmp_path, monkeypatch
) -> None:
    from prosaic_runtime.openai_compatible import OpenAICompatibleBackend

    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __init__(self) -> None:
            self._lines = iter([
                b'  data: {"choices":[{"delta":{"content":"echelon_result:\\n"}}]}\n',
                b'  data: {"choices":[{"delta":{"content":"  verdict: PASS\\n"},"finish_reason":"stop"}],"usage":{"total_tokens":35}}\n',
                b"  data: [DONE]\n",
            ])

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def readline(self):
            return next(self._lines, b"")

    monkeypatch.setattr(
        "prosaic_runtime.openai_compatible.urllib.request.urlopen",
        lambda request, timeout: FakeResponse(),
    )

    result = OpenAICompatibleBackend(_openai_config()).run_prompt(
        CliRunRequest(
            cwd=str(tmp_path),
            prompt="Return a result.",
            env={},
            timeout_s=12.5,
        )
    )

    assert result.exit_code == 0
    assert result.stdout == "echelon_result:\n  verdict: PASS\n"
    assert result.token_usage == 35


def test_openai_compatible_backend_accepts_case_insensitive_done_marker(
    tmp_path, monkeypatch
) -> None:
    from prosaic_runtime.openai_compatible import OpenAICompatibleBackend

    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __init__(self) -> None:
            self._lines = iter([
                b'data: {"choices":[{"delta":{"content":"echelon_result:\\n  verdict: PASS\\n"},"finish_reason":"stop"}],"usage":{"total_tokens":37}}\n',
                b"data: [Done]\n",
            ])

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def readline(self):
            return next(self._lines, b"")

    monkeypatch.setattr(
        "prosaic_runtime.openai_compatible.urllib.request.urlopen",
        lambda request, timeout: FakeResponse(),
    )

    result = OpenAICompatibleBackend(_openai_config()).run_prompt(
        CliRunRequest(
            cwd=str(tmp_path),
            prompt="Return a result.",
            env={},
            timeout_s=12.5,
        )
    )

    assert result.exit_code == 0
    assert result.stdout == "echelon_result:\n  verdict: PASS\n"
    assert result.token_usage == 37


def test_openai_compatible_backend_records_streaming_response_metadata(
    tmp_path, monkeypatch
) -> None:
    from prosaic_runtime.openai_compatible import OpenAICompatibleBackend

    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __init__(self) -> None:
            self._lines = iter([
                b'data: {"id":"chatcmpl-stream","object":"chat.completion.chunk","created":1725000001,"model":"local-model","system_fingerprint":"fp_stream","choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n',
                b"data: [DONE]\n",
            ])

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def readline(self):
            return next(self._lines, b"")

    monkeypatch.setattr(
        "prosaic_runtime.openai_compatible.urllib.request.urlopen",
        lambda request, timeout: FakeResponse(),
    )

    result = OpenAICompatibleBackend(_openai_config()).run_prompt(
        CliRunRequest(
            cwd=str(tmp_path),
            prompt="Return a result.",
            env={},
            timeout_s=12.5,
        )
    )

    assert result.exit_code == 0
    assert result.metadata["request_model"] == "local-model"
    assert result.metadata["raw_response_metadata"] == {
        "id": "chatcmpl-stream",
        "object": "chat.completion.chunk",
        "created": 1_725_000_001,
        "model": "local-model",
        "system_fingerprint": "fp_stream",
    }


def test_openai_compatible_backend_rejects_streaming_truncation(
    tmp_path, monkeypatch
) -> None:
    from prosaic_runtime.openai_compatible import OpenAICompatibleBackend

    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __init__(self) -> None:
            self._lines = iter([
                b'data: {"choices":[{"delta":{"content":"echelon_result:\\n  verdict:"},"finish_reason":"length"}],"usage":{"total_tokens":37}}\n',
                b"data: [DONE]\n",
            ])

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def readline(self):
            return next(self._lines, b"")

    monkeypatch.setattr(
        "prosaic_runtime.openai_compatible.urllib.request.urlopen",
        lambda request, timeout: FakeResponse(),
    )

    result = OpenAICompatibleBackend(_openai_config()).run_prompt(
        CliRunRequest(
            cwd=str(tmp_path),
            prompt="Return a result.",
            env={},
            timeout_s=12.5,
        )
    )

    assert result.exit_code == 1
    assert result.stdout == "echelon_result:\n  verdict:"
    assert result.token_usage == 37
    assert "finish_reason=length" in result.stderr
    assert result.metadata["streamed"] is True
    assert result.metadata["provider_error_code"] == "incomplete_generation"


def test_openai_compatible_backend_parses_unlabeled_sse_body(
    tmp_path, monkeypatch
) -> None:
    from prosaic_runtime.openai_compatible import OpenAICompatibleBackend

    class FakeResponse:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return (
                b'data: {"choices":[{"delta":{"content":"echelon_result:\\n"}}]}\n\n'
                b'data: {"choices":[{"delta":{"content":"  verdict: PASS\\n"},"finish_reason":"stop"}],"usage":{"total_tokens":17}}\n\n'
                b"data: [DONE]\n\n"
            )

    monkeypatch.setattr(
        "prosaic_runtime.openai_compatible.urllib.request.urlopen",
        lambda request, timeout: FakeResponse(),
    )

    result = OpenAICompatibleBackend(_openai_config()).run_prompt(
        CliRunRequest(
            cwd=str(tmp_path),
            prompt="Return a result.",
            env={},
            timeout_s=12.5,
        )
    )

    assert result.exit_code == 0
    assert result.stdout == "echelon_result:\n  verdict: PASS\n"
    assert result.token_usage == 17
    assert result.metadata["streamed"] is True


def test_openai_compatible_backend_streams_multiline_sse_events(
    tmp_path, monkeypatch
) -> None:
    from prosaic_runtime.openai_compatible import OpenAICompatibleBackend

    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __init__(self) -> None:
            self._lines = iter([
                b'data: {"choices":[\n',
                b'data: {"delta":{"content":"echelon_result:\\n"}}\n',
                b"data: ]}\n",
                b"\n",
                b'data: {"choices":[{"delta":{"content":"  verdict: PASS\\n"},"finish_reason":"stop"}],"usage":{"total_tokens":19}}\n',
                b"data: [DONE]\n",
            ])

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def readline(self):
            return next(self._lines, b"")

    monkeypatch.setattr(
        "prosaic_runtime.openai_compatible.urllib.request.urlopen",
        lambda request, timeout: FakeResponse(),
    )

    result = OpenAICompatibleBackend(_openai_config()).run_prompt(
        CliRunRequest(
            cwd=str(tmp_path),
            prompt="Return a result.",
            env={},
            timeout_s=12.5,
        )
    )

    assert result.exit_code == 0
    assert result.stdout == "echelon_result:\n  verdict: PASS\n"
    assert result.token_usage == 19


def test_openai_compatible_backend_rejects_streamed_tool_calls(
    tmp_path, monkeypatch
) -> None:
    from prosaic_runtime.openai_compatible import OpenAICompatibleBackend

    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __init__(self) -> None:
            self._lines = iter([
                b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"write_file","arguments":"{}"}}]}}]}\n',
            ])

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def readline(self):
            return next(self._lines, b"")

    monkeypatch.setattr(
        "prosaic_runtime.openai_compatible.urllib.request.urlopen",
        lambda request, timeout: FakeResponse(),
    )
    backend = OpenAICompatibleBackend(_openai_config())
    result = backend.run_prompt(
        CliRunRequest(
            cwd=str(tmp_path),
            prompt="Return a result.",
            env={},
            timeout_s=12.5,
        )
    )

    assert result.exit_code == 1
    assert "tool_calls are not supported" in result.stderr
    assert result.metadata["provider_error_code"] == "unsupported_tool_calls"


def test_openai_compatible_backend_stream_read_timeout_returns_timed_out(
    tmp_path, monkeypatch
) -> None:
    from prosaic_runtime.openai_compatible import OpenAICompatibleBackend

    class FakeResponse:
        headers = {"Content-Type": "text/event-stream"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def readline(self):
            raise socket.timeout("stream stalled")

    monkeypatch.setattr(
        "prosaic_runtime.openai_compatible.urllib.request.urlopen",
        lambda request, timeout: FakeResponse(),
    )
    backend = OpenAICompatibleBackend(_openai_config())
    result = backend.run_prompt(
        CliRunRequest(
            cwd=str(tmp_path),
            prompt="Return a result.",
            env={},
            timeout_s=12.5,
        )
    )

    assert result.exit_code == -1
    assert result.timed_out is True
    assert "stream stalled" in result.stderr
    assert result.metadata["provider_error_code"] == "timeout"
