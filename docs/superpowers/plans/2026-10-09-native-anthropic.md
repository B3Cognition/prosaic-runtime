# Native Anthropic Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for native execution, or superpowers:subagent-driven-development if the user selects delegation. Steps use checkbox syntax for tracking.

**Goal:** Execute neutral Prosaic text and tool tasks through native Anthropic Messages.

**Architecture:** Extract the bounded conversation loop into a shared internal engine.
OpenAI and Anthropic adapters encode requests and parse provider turns; Runtime
continues to own grants, limits, tool execution, cancellation, and accounting.
Retain OpenAICompatibleBackend as an integration facade with its existing hooks.

**Tech Stack:** Python 3.11+, urllib HTTP/SSE, dataclasses, PyYAML, pytest.

**Spec:** `docs/superpowers/specs/2026-10-09-native-anthropic-design.md`

## Global Constraints

- Existing YAML and positional EndpointConfig construction retain their meanings.
- Keep existing public Result, Invocation, RunPolicy, event, and serialized ledger contracts compatible.
- Preserve the low-level OpenAICompatibleBackend integration seam used by Echelon.
- Provider adapters cannot execute tools or expand grants.
- No ledger schema or PostgreSQL migration is required.
- No Node or TypeScript execution; no live paid calls for automated verification.
- Use apply_patch for edits; preserve unrelated changes and historic evidence.

## Review Focus

- Mixed provider profiles must select credentials, protocol, and accounting independently.
- Streamed tool JSON must remain inert until complete message framing is validated.
- Multiple tool results must retain their IDs and precede the next ordinary user input.
- Cache creation and unfamiliar usage categories must never yield an incorrect trusted cost.
- Acquisition final prompts and no-tool calls must retain the same limits as tool turns.

## Task 1: Extract execution seams with OpenAI regressions

**Files:** Create `src/prosaic_runtime/provider_types.py` and
`src/prosaic_runtime/execution.py`; modify `openai_compatible.py`, `runtime.py`;
add `tests/test_provider_execution.py`.

**Interfaces:** Internal `ProviderTurn` carries current completion-turn fields
plus `continuation: object | None`. Shared `ExecutionBackend` owns run_prompt and
the bounded loop, calling encode/send hooks. Preserve existing facade signatures
`_chat_payload(messages, metadata, *, streaming, tools)` and
`_post_chat_turn(payload, request, deadline, streaming)` for OpenAI consumers.

- [x] Add regression tests using the local server fixture in test_runtime.py:

```python
def test_openai_facade_hooks_remain_overrideable():
    from prosaic_runtime.openai_compatible import OpenAICompatibleBackend
    assert callable(OpenAICompatibleBackend._chat_payload)
    assert callable(OpenAICompatibleBackend._post_chat_turn)
```

Also pin no-tool JSON and SSE wire payloads, first-tool enforcement, acquisition
final transitions, and custom tool execution using current public Runtime tests.
- [x] Run `.venv/bin/python -m pytest tests/test_provider_execution.py tests/test_runtime.py tests/test_acquisition.py tests/test_transport.py -q` before extraction to establish passing behavior.
- [x] Move loop code without changing grants or wire requests. Replace the
completion-turn definition with a compatibility alias to ProviderTurn; introduce
continuation conversion hooks so native blocks need not be reconstructed.
Route bounded no-tool calls through the same request boundary as tool calls.
- [x] Run the command above plus the full suite; investigate any changed result,
event, request, or token count rather than updating expected values blindly.
- [x] Commit only these changes as `refactor: separate provider transport from execution`.

## Task 2: Native configuration and JSON Messages execution

**Files:** Modify `config.py`, `runtime.py`, `diagnostics.py`; create
`anthropic.py`; add `tests/test_anthropic.py`; modify `tests/test_config.py`.

**Interfaces:** Append `provider: str = 'openai-compatible'` to EndpointConfig.
AnthropicBackend uses shared execution hooks and emits ProviderTurn. Runtime
selects the backend from endpoint.provider without changing public call signatures.

- [x] Add failing configuration tests:

```python
def test_provider_defaults_and_unknown_provider():
    assert EndpointConfig('http://localhost/v1', 'm').provider == 'openai-compatible'
    with pytest.raises(ValueError, match='provider'):
        EndpointConfig('http://localhost/v1', 'm', provider='unknown')
```

Add a local HTTP server assertion that an Anthropic text call sends `/v1/messages`,
`x-api-key`, `anthropic-version`, no Bearer header, system separately, and positive
max_tokens. Return a complete native text response and assert public Result text.
- [x] Run `.venv/bin/python -m pytest tests/test_config.py tests/test_anthropic.py -q`; new native tests must fail before implementation.
- [x] Implement native payload conversion and JSON validation. Convert assistant
tool requests to tool_use blocks and tool results to user tool_result blocks;
retain complete continuation content. Reject unsupported enabled json_mode,
reasoning controls, and non-text native blocks before continuing execution.
- [x] Add two-turn read_file, multiple-tool IDs, custom-tool, forced first-tool,
denied-grant, and acquisition tests. Assert no tool executes on truncation,
refusal, malformed arguments, or unexpected forced calls. Test mixed profiles.
- [x] Run native tests and existing runtime, acquisition, custom-tool, and
diagnostics tests; commit as `feat: add native Anthropic Messages execution`.

## Task 3: Strict native streaming and bounded failures

**Files:** Create `anthropic_stream.py`; modify `anthropic.py`; add
`tests/test_anthropic_stream.py` and expand `tests/test_anthropic.py`.

**Interfaces:** `read_anthropic_turn(response, deadline, *, on_text)` returns
ProviderTurn or Result. It uses the bounded response wrapper and cancellation
checks; on_text emits provisional text through the existing event mechanism.

- [x] Add a failing fragmented-input stream test using these native events:

```python
events = [
    {'type': 'content_block_start', 'index': 0, 'content_block':
     {'type': 'tool_use', 'id': 't1', 'name': 'read_file', 'input': {}}},
    {'type': 'content_block_delta', 'index': 0, 'delta':
     {'type': 'input_json_delta', 'partial_json': '{"path":'}},
    {'type': 'content_block_delta', 'index': 0, 'delta':
     {'type': 'input_json_delta', 'partial_json': '"evidence.txt"}'}},
    {'type': 'content_block_stop', 'index': 0},
]
```

Wrap these in message_start, message_delta(stop_reason=tool_use), and message_stop;
assert the tool executes only after message_stop. Removing that terminal event
must produce failure with zero executed tools.
- [x] Run `.venv/bin/python -m pytest tests/test_anthropic_stream.py -q` and verify new tests fail.
- [x] Implement block-index state validation, text/input deltas, ping handling,
SSE multiline framing, complete message termination, and explicit stream errors.
Reject duplicate/reordered block events, unsupported blocks, non-object input,
invalid UTF-8/JSON, and unknown consequential event types. Do not retry.
- [x] Add public Runtime tests for cancellation, timeout, redirects, HTTP errors,
input/response size overflow, missing terminal markers, refusal, max_tokens, and
pause_turn. Check no-tool and tool paths share all bounds. Cross-check parser
event fields against official Anthropic documentation.
- [x] Run native streaming plus test_redirect_safety.py, test_input_boundary.py,
and test_features.py; commit as `feat: support bounded Anthropic streaming`.

## Task 4: Provider-aware usage and durable evidence

**Files:** Modify `accounting.py`, `accounting_capture.py`, `runtime.py`;
create `anthropic_usage.py`; add `tests/test_anthropic_accounting.py`.

**Interfaces:** `normalize_anthropic_usage(raw: object) -> dict` returns the existing
canonical usage keys/status. Capture chooses an observer from endpoint.provider;
its OpenAI default remains unchanged. Native raw usage stays in Result metadata.

- [x] Add failing tests for native usage categories:

```python
def test_cache_creation_cannot_be_priced_as_ordinary_input():
    usage = normalize_anthropic_usage({
        'input_tokens': 10, 'output_tokens': 4,
        'cache_read_input_tokens': 6, 'cache_creation_input_tokens': 8})
    assert usage['input_tokens'] == 24
    assert usage['total_tokens'] == 28
    assert usage['cached_input_tokens'] == 6
    assert usage['status'] == 'unsupported'
```

Add snapshots with output 1 then 7; final output must be 7, not 8. Add partial
stream, conflicting IDs, invalid counters, unknown nonzero categories, and
recorder-provider mismatch tests (mismatch must make zero HTTP calls).
- [x] Run `.venv/bin/python -m pytest tests/test_anthropic_accounting.py -q`; confirm failures.
- [x] Implement native usage normalization and message event observation. Capture
native IDs/model before cancellation callbacks; require complete native terminal
framing. Preserve ledger shape, OpenAI normalization, and existing pricing rules.
Validate recorder/provider match before opening transport. Never guess absent
usage or assign unsupported cache creation ordinary-input prices.
- [x] Run native accounting tests, test_accounting.py, test_usage_integrity.py,
and available PostgreSQL adapter tests; commit as `feat: capture Anthropic usage safely`.

## Task 5: Documentation and installed-package verification

**Files:** Modify `README.md`, `CHANGELOG.md`, `pyproject.toml`; add
`examples/anthropic.yml` and `tests/test_anthropic_package.py`.

- [x] Add a configuration example with provider anthropic, operator-selected model,
base_url https://api.anthropic.com/v1, api_key_env ANTHROPIC_API_KEY, and explicit
allowed_tools. Test loading it without needing credentials or network access.
- [x] Document text/tool support, unsupported features, protocol selection,
credential configuration, cached-input accounting limits, and optional live smoke.
Update package description without changing release version implicitly.
- [x] Run `.venv/bin/python -m pytest` in Runtime. Run adapter tests when their
dependencies are available; report unavailable external services explicitly.
- [x] Run `.venv/bin/python -m build`. Runtime has no scripts/wheel_smoke.py:
install the built wheel into a temporary virtualenv, run imports/config loading,
`prosaic-runtime --help`, and native JSON/text-tool fixture tests from outside
the checkout with PYTHONPATH unset. Confirm imports resolve to installed files.
- [x] Inspect the final diff for secrets, unintended format changes, duplicated
permission logic, and public hook breakage. Commit documentation and tests as
`docs: document native Anthropic provider support`; report verification evidence.

## Self-review

All spec sections map to the five tasks: compatibility/execution (1), native
configuration/tools (2), streaming/failures (3), usage/accounting (4), and package
verification/documentation (5). Review Focus conditions are assigned to tasks
2–4. No new public provider plugin interface or ledger schema is introduced.
Native execution selected by the user. Implementation and package verification
are complete; final independent review is pending. Full suite: 453 passed,
1 skipped. Installed wheel: 89 native tests passed outside the checkout.
