# Changelog

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
