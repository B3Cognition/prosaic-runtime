from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request

from harness.ai_cli_backend import CliRunRequest, CliRunResult
from harness.config import HarnessConfig


class OpenAICompatibleBackend:
    name = "openai-compatible"

    def __init__(self, config: HarnessConfig) -> None:
        self._config = config

    def run_prompt(self, request: CliRunRequest) -> CliRunResult:
        llm = self._config.llm
        assert llm.base_url is not None
        assert llm.model is not None
        payload: dict[str, object] = {
            "model": llm.model,
            "messages": [{"role": "user", "content": request.prompt}],
            "temperature": llm.temperature,
        }
        if llm.max_tokens is not None:
            payload["max_tokens"] = llm.max_tokens
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        token = _api_key(llm.api_key_env)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        http_request = urllib.request.Request(
            f"{llm.base_url.rstrip('/')}/chat/completions",
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                http_request, timeout=request.timeout_s
            ) as response:
                body = response.read().decode("utf-8", errors="replace")
        except TimeoutError as exc:
            return CliRunResult(
                exit_code=-1,
                stdout="",
                stderr=str(exc),
                timed_out=True,
                metadata={"provider": self.name},
            )
        except socket.timeout as exc:
            return CliRunResult(
                exit_code=-1,
                stdout="",
                stderr=str(exc),
                timed_out=True,
                metadata={"provider": self.name},
            )
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return CliRunResult(
                exit_code=int(exc.code),
                stdout="",
                stderr=body or str(exc),
                metadata={"provider": self.name, "http_status": int(exc.code)},
            )
        except urllib.error.URLError as exc:
            return CliRunResult(
                exit_code=1,
                stdout="",
                stderr=str(exc.reason),
                metadata={"provider": self.name},
            )
        except OSError as exc:
            return CliRunResult(
                exit_code=1,
                stdout="",
                stderr=str(exc),
                metadata={"provider": self.name},
            )
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return CliRunResult(
                exit_code=1,
                stdout="",
                stderr=f"Malformed OpenAI-compatible response: {body}",
                metadata={"provider": self.name},
            )
        text = _assistant_text(parsed)
        if text:
            print(text, flush=True)
        return CliRunResult(
            exit_code=0,
            stdout=text,
            stderr="",
            token_usage=_token_usage(parsed),
            metadata={"provider": self.name},
        )

    def run_agent(self, request: CliRunRequest) -> CliRunResult:
        return self.run_prompt(request)


def _api_key(api_key_env: str | None) -> str:
    if not api_key_env:
        return ""
    return os.environ.get(api_key_env, "")


def _assistant_text(parsed: object) -> str:
    if not isinstance(parsed, dict):
        return ""
    choices = parsed.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
    text = first.get("text")
    return text if isinstance(text, str) else ""


def _token_usage(parsed: object) -> int:
    if not isinstance(parsed, dict):
        return 0
    usage = parsed.get("usage")
    if not isinstance(usage, dict):
        return 0
    total = usage.get("total_tokens")
    if isinstance(total, int):
        return total
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    total_tokens = 0
    if isinstance(prompt, int):
        total_tokens += prompt
    if isinstance(completion, int):
        total_tokens += completion
    return total_tokens
