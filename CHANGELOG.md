# Changelog

## Unreleased

- Immutable host-registered `CustomTool` API, strict closed-schema validation,
  finite JSON/result bounds, authorization predicate, native HTTP/SSE execution,
  versioned structural events, and a runnable catalogue example. Registration
  grants no authority; prose, YAML and host policy must agree.
- Callbacks are trusted synchronous code, not sandboxed or forcibly preemptible.
  Fixed safe error envelopes have an independent 256-byte ceiling, even when
  the configured successful-result bound is smaller.
- Stage-1 source verification: 207 Runtime / 101 Harness tests, real local
  HTTP/SSE examples, both package builds. All four TokenProxy models tested in
  both modes; 6/8 Runtime invocations and 6/8 Harness cases succeeded/admitted.
  Explicit-first-tool failures and fabricated no-tool answers stayed blocked;
  no admission rule was relaxed. Wider adversarial hardening/MCP remain queued.
- Fresh review fixes: preserve malformed custom-call shapes through HTTP/SSE
  parsing (including invalid stream fragments), and redact callback cancellation
  messages while retaining exit 130/known usage. Final source tests: 220 Runtime /
  101 Harness. A second unchanged-policy all-model matrix succeeded in 5/8 Runtime
  cases and admitted 6/8 Harness cases; zero-call endpoint misses stayed blocked.

## 0.4.0 — 2026-10-01

- Prosaic inspection deadlines return an unsuccessful `inspection_timeout`
  result instead of leaking a subprocess timeout exception from `run()`.

- Public Runtime inference rejects HTTP redirects rather than forwarding
  endpoint bearer credentials to another destination. Diagnostics already
  rejected redirects; real two-server tests cover tool and no-tool inference.

- No-tool HTTP requests now enforce the serialized input-byte limit, including
  request overhead, just like tool-loop requests.

- Public usage totals stay unknown when any completed turn lacks complete,
  nonnegative integer usage. Genuine reported zero remains zero; known partial
  sums are diagnostic metadata, not a complete invocation total.

- Opt-in Prosaic `acquisition` artifact: perform an explicitly granted first
  native tool call before disclosing full agent prose, resources and arguments.
  One conversation/model/deadline/tool budget; failed acquisition blocks with
  no fallback. `acquisition_v1` capability, HTTP/SSE ordering and denial tests,
  staged and explicit no-tool preloading examples. Completed-turn reported usage
  is retained when a later cancellation, input limit or timeout stops execution.

- An explicit initial tool choice now fails with `tool_choice_not_honored` if
  the endpoint omits, substitutes or adds first-turn calls. No returned call is
  executed on a mismatch and no automatic retry is made. Reported usage is kept.
  Automatic tool selection remains optional. `initial_tool_enforcement_v1`
  advertises the stricter contract.

## 0.3.0 — 2026-09-30

- Successful `read_file` results and events bind the file path, byte SHA-256,
  offset, lines read and total line count. Structural events omit file content
  and raw arguments.
- `RunPolicy.initial_tool` requests a granted function on the first turn only;
  permission intersection remains unchanged. Capability flags let consumers
  require read provenance and initial-tool support explicitly.
- Four-tier Markdown examples, a larger synthetic launch dossier, TokenProxy
  YAML profiles and `examples/run_tiers.py` demonstrate independent bounded
  agents with and without read tools. This is not recursive agent execution.
- Live basic native-read probes passed for all four TokenProxy model IDs with
  streaming on/off and automatic/explicit tool choice (16 combinations).
- Known limitation: some richer reader prompts returned text without the
  explicitly requested tool call. This version requests tool choice but does
  not itself reject its omission; consumers must verify tool execution/events.
  A successful read does not establish answer correctness.

## 0.2.0 — 2026-09-29

### Breaking changes

- Runtime configuration now uses YAML/YML exclusively. TOML support was removed
  without a migration layer. Default discovery checks `prosaic-runtime.yaml`,
  then `prosaic-runtime.yml`. PyYAML is now a runtime dependency.
- CLI invocations now show status and elapsed-time progress on stderr by default;
  `--quiet` suppresses progress. JSON output remains on stdout.

### Added

- `doctor` checks configuration, Prosaic availability, credentials and model
  discovery; inference requires explicit `--inference`.
- `smoke --live` runs packaged no-tool and read-tool probes with synthetic evidence,
  reporting completion, observed streaming and actual tool execution independently.
- `--output text` prints the answer without a JSON wrapper.
- YAML `limits.timeout_s` and `limits.max_tool_rounds`, with CLI overrides.
- Structured `tool_started` and `tool_completed` events.
- A complete first-run guide, runnable examples with and without tools, and
  `examples/run_examples.py` with in-memory endpoint/model overrides.

### Licensing and verification

- Project license is Apache-2.0; historical MIT attribution is retained.
- Expanded automated coverage for YAML loading, permissions, diagnostics, CLI
  output, example programs and live-smoke reporting.
- Live verification performed with ThinkingCap-Qwen3.6-27B-OptiQ-4bit:
  authenticated discovery, streamed completion, a real file-tool round trip,
  and plain-text CLI output passed. No credentials are included in this release.

## 0.1.0 — 2026-09-29

- Initial extraction of generic OpenAI-compatible execution from Echelon,
  with a Prosaic-only Python API and CLI, streaming, bounded file tools,
  explicit permissions, events and transport conformance tests.
