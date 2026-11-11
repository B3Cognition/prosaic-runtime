# Changelog

## Unreleased

- A `fix` guard that declines to repair (a violation with `fixed=None`) now takes the rule retry path instead of
  failing the turn as a guard fault. One extra provider request is possible, within `RunPolicy` budgets; the rule is
  reported in the new `TurnResult.rules_declined` instead of `rules_faulted`. `TurnResult.rules_retried` lists rules
  answered with a granted retry. Unusable repairs and raising guards remain faults.
- Add opt-in `ProsaicRuntime(..., correct_malformed_tool_calls=True)` (default off): a conversation turn may ask the
  model once to resend a tool call that batch admission refused, with a closed-code envelope and within the request
  budget, instead of failing with `invalidToolRequest`. `TurnResult.tool_request_corrections` reports it.
  Response-level malformation stays terminal. Default turns keep their prepared-turn bytes.

## 0.9.21 — 2026-11-11

- Add bounded, opt-in parallel execution for trusted custom read tools on both supported API wires.
- Preserve ordered guards, evidence, citations and journal outcomes with cooperative cancellation.
- See [release notes](docs/release-0.9.21.md).

## 0.9.20 — 2026-11-10

- Add opt-in safe scalar tool events, typed private tool results and actual-delivery evidence.
- Validate final citations after guards; preserve complete private guard facts and old disabled behavior.
- Preserve known completion on event/capacity failure and bound the complete registry atomically.
- Keep Core/recorder bytes unchanged; see [release notes](docs/release-0.9.20.md).

## 0.9.19 — 2026-11-09

- Add explicit input profiles and independent private guard facts with fail-only feedback.
- Fit evidence variants and retained history within one bounded context-work budget.
- Restore frozen v3 turns without recapture, recomposition or counting; preserve legacy payloads and identities by default.
- Export `guard_input_views_v1`, `frozen_turn_inputs_v1` and `context_input_variants_v1`.
- Core and the published PostgreSQL recorder remain unchanged. See [release notes and qualification](docs/release-0.9.19.md).

See [published release notes and compatibility](docs/releases.md), including backfilled historical release notes. The documentation-only refresh on 2026-11-07 preserved the packages and archives published at that time. Versioned changes below describe later releases; old artifacts remain immutable.

## 0.9.17

- Add bounded immutable trusted-host accounting run/variant labels through `ExecutionContext.request_metadata` and public capability `accounting_request_metadata_v1`.
- Recorder prepare sees the versioned optional context extension before each request; both supported wires, tool/acquisition/repair paths and saved Harness resume retain labels without provider disclosure.
- Empty metadata preserves legacy accounting bytes/hashes and all artifact/workflow identities; present labels intentionally change accounting payload hashes. Same-call relabeling conflicts, saved run labels win.
- Retain published recorder 0.3.0 code/schema/privileges and old rows. Labeled contexts require Runtime >=0.9.17 readers. See [release notes](docs/release-0.9.17.md).

## 0.9.16

- Add bounded host-only `request_headers` to OpenAI-compatible `run` and `turn`, including prepared turns, tool continuations and rule retries.
- Snapshot headers before callbacks; preserve credentials/framing/correlation ownership, redirect refusal and per-invocation isolation. Raw custom headers are not recorded or inserted into prompts.
- Identity impact: no new artifact, workflow, prepared-turn or rule identity fields; omitted/empty options preserve prior behavior. Existing guard versions are unchanged.
- Document the approved validator V1 and remaining Cerebro requests. New version-specific release notes accompany publication.

## 0.9.15 — 2026-11-06

- Enforce suffix currency codes, euro/euros and cent/cents in
  `value_in_tool_output`; cent(s) is a hundredth, not a major unit or x-multiple.
- Use the same bounded answer/source scanner, preserving strict signs,
  currency identity, precision and malformed-source refusal. Overlapping markers
  cannot hide an unsupported amount; ambiguous compound units give no evidence.
- Keep whole-word and Markdown-bullet controls. Add unit and both-wire fee retry
  regressions and run them against the transported wheel in every release cell.
- Advance the value guard to `value-in-tool-output-v2`, so affected bindings
  require explicit readmission. Other guards and untagged behavior stay unchanged.
- Keep Core 0.5.0 and PostgreSQL recorder 0.3.0; publish only qualified Runtime
  bytes. See [release scope](docs/release-0.9.15.md).

## 0.9.14 — 2026-11-06

- Add opt-in rule admission for conversation turns, consuming Prosaic Core's
  inert enforcement tags (`rules`). Hosts register `Guard`s in Python; prose
  binds only registered guards. Off by default: untagged agents are
  byte-identical, pinned by a golden capture from the previous main.
- Refuse turn admission for unregistered guards (always for `on_violation=fail`;
  otherwise unless `allow_unbound_guards=True`), rule issues, invalid settings,
  unsupported stages, `fix` bindings on guards not declaring `fixes=True`, path
  settings outside the project and ambiguous multi-file bindings (resolved per
  guard and stage; different settings at one stage are refused whatever the
  actions, so composition never drops a check). Turn identity records project-relative paths, so it is
  portable across machines. Add `RuntimeConfig.required_guards`: minimum actions,
  optionally with host-authoritative pinned settings and stage.
- Run guards at `before_tool`, `after_tool`, `before_answer` and `final` inside
  the existing loop with `fix` (bounded, rechecked), one `retry` per turn within
  existing budgets (feedback quotes the rule and refused values as unprivileged
  data) and `fail` (new failure code `ruleViolation`). Guarded Harness
  conversations are unsupported until a companion Harness release preserves
  `required_guards`, inspection rules and guard registrations, and admits
  `ruleViolation`.
- Guards see successful custom, CLI and built-in tool results as facts without
  envelope bookkeeping, so data read with `read_file` and other built-ins counts.
  Undeliverable feedback values (oversized integers, unencodable text) are guard
  faults, reported in `rules_faulted` before any further provider request;
  feedback stays within 16 KB so a retry always fits. In turns with bound rules,
  tool arguments with a lone surrogate are `invalidToolRequest` on both wires.
- While answer-stage rules are bound, draft text is held back from observers
  and console previews until accepted, then delivered as an untagged turn would
  deliver it; a refused draft is never delivered.
- Add `TurnResult.rules_evaluated`, `rules_violated`, `rules_fixed`,
  `rules_faulted` (ids only) and `rules_fingerprint`; send the model Core's
  `strip_tags` text. Add `ProsaicRuntime.rule_coverage()` and
  `RuleSet.coverage()` to report enforcement coverage without a turn.
- Answer-stage retry feedback sends raw refused values only for guards
  registered with `echo_values=True`; others send a count.
- `run()`, acquisitions and `NativeRuntime.run` refuse artifacts with enforce
  tags (including composed inspection rules) rather than run them unenforced;
  `run()` also refuses while `required_guards` is configured.
- Ship four generic guards, inert until registered and bound:
  `no_wrapped_links`, `value_in_tool_output`, `args_reference_seen_ids`,
  `strict_json`. `args_reference_seen_ids` checks ids of any length, refuses
  empty ones, finds pattern ids inside text and object keys, accepts numeric
  ids only from id fields or an id cue in the message (never counts or page
  numbers), and takes whole-word evidence only from the message and built-in
  file reads.
  `value_in_tool_output` reads common formats (signs, accounting parentheses,
  scales such as `m`/`bn`/`million`/`crore`, space and apostrophe grouping,
  European decimals, prefixed dollars, trailing symbols), refuses ranges and
  unreadable figures, never reads a bullet as a minus, matches by rounding only, and
  requires the sign of amounts and x-multiples to agree unless `sign=magnitude`;
  marked figures it cannot parse are refused in the answer and give no evidence
  in tool results (one figure grammar and boundary check for both).
  `no_wrapped_links` never rewrites code and handles link titles and
  parenthesised targets. Uses Core 0.5.0's `parse_rules`; an older Core refuses
  tagged artifacts and leaves untagged ones unchanged. The new
  `scripts/rule_admission_wheel_smoke.py` runs a tagged agent in release
  qualification against the installed Core.

## 0.9.13 — 2026-11-05

- Require Prosaic >=0.5.0,<0.6 and qualify its exact published artifacts.
- Update current installation and release pins; preserve provider contracts and
  the separately published PostgreSQL recorder 0.3.0.

## 0.9.12 — 2026-11-02

- Add canonical `effort: none`. Map Gemma 4 none/positive efforts to its native
  thinking switch; keep ultra Codex-only and preserve tiered model controls.
- Recognize verified local ThinkingCap Qwen aliases and turn thinking off
  explicitly for none, avoiding server-side effort alias fallback.
- Add local Markdown examples and live control verification evidence.

## 0.9.11 — 2026-11-02

- Add opt-in host-owned terminal tool completion with an independently bound
  inline artifact, native provider accounting and no acknowledgement model call.
- Admit terminal authority before effects, reject unsafe multi-call/journal and
  unsupported transport combinations, and retain known completion on cancellation.
- Prevent RuntimeAdapter redispatch or replacement of a terminal delegate result.

## 0.9.10 — 2026-11-02

- Map canonical Markdown `effort` to OpenAI-compatible, Claude Code and Codex
  native controls, consistently overriding profile defaults.
- Support `xhigh`, `max` and Codex-only `ultra` using bounded native metadata
  discovery and documented model defaults. Retain optional `supported_efforts`
  overrides; reject unavailable levels without silently changing effort.
- Add credential-free integration tests and runnable DeepSeek V4, Qwen 3.8 and
  Nemotron NIM examples with automatic provider/model capabilities.
- Preserve existing API `none` settings and completion token dialect controls.

## 0.9.9 — 2026-10-29

- Add policy-admitted model-free `run_tool()` with schema checks, shared tool budgets,
  cancellation and effect/accounting evidence; no provider request is created.

- Add validated OpenAI-compatible `completion_token_parameter` endpoint capability.
  Keep semantic output caps and context/strict-budget reservation under either wire name.
- Support `none`, `xhigh` and `max` API reasoning settings and omit unset temperature,
  including constrained prompt requests. Legacy profiles keep their default wire name.

## 0.9.8 — 2026-10-24

- Preserve bounded CLI output errors when Darwin denies a signal during child
  exit; suppress the cleanup denial only after bounded reaping and proof that
  the whole process group is absent.

- Prevent native fallback after partial text, reasoning, redacted thinking or
  streamed model activity, even if a later quota error reports zero turns.

- Expose unified pools through the CLI with independent native permissions/API
  grants and explicit total/backend deadlines and attempt/request/tool budgets.
- Add coherent, credential-free native/local configuration and agent examples.

- Add opt-in UnifiedRuntime pools across native Claude/Codex and API/local
  profiles, with separate explicit policies/budgets, capability filtering,
  shared quota cooldowns and transport-proven pre-work switching only.

- Add opt-in NativeRuntime with Claude Code/Codex CLI profiles, explicit native
  permissions, bounded JSONL subprocess execution and process-group cleanup.
- Add priority profile pools, account-shared capacity cooldowns and atomic
  optional file persistence; restrict automatic fallback to failures before work.
- Preserve partial/unknown usage and expose recovery-required interruptions.
- Add native CLI configuration and profile/pool selection without changing API
  endpoint configuration, conversations, tool grants or Harness capabilities.

## 0.9.6 — 2026-10-17

- Add opt-in host-controlled final-answer phases after a bounded number of tool rounds.
- Add optional oMLX native tool-call envelope constraints, removed for final answers.
- Validate envelope configuration before dispatch and reject further tool calls after finalization.
- Retain native grant, budget and argument checks; defaults remain unchanged.

## 0.9.3 — 2026-10-14

- Normalize negative provider timeout exit statuses to 124 in conversation turn
  results so Harness can persist failed timeout receipts without `store_corrupt`.
- Preserve existing run/CLI timeout statuses and the conversation storage format.
- Cover streaming and nonstreaming provider timeouts through the public turn API.

## 0.9.2 — 2026-10-13

- Add a host-owned ExecutionBudget that shares deadlines, provider/tool dispatch
  allowances and known/unknown reported-token evidence across nested calls/retries.
- Preserve trusted parent accounting context in tool callbacks and expose bounded
  child lineage and remaining-time helpers without placing attribution in prompts.
- Qualify SDK and existing PostgreSQL recorder compatibility; publish only the SDK.

## 0.9.1 — 2026-10-13

- Add versioned `RuntimeAdapter` for trusted host projections and normalization
  around an unmodified Runtime. Execution callbacks can only reduce authority.
- Share deadline, request/tool allowances and reported-token evidence across all
  adapter attempts; preserve known partial and unknown usage and callback failures.
- Ordinary execution, conversation APIs, accounting and PostgreSQL remain unchanged.
- Publish only the SDK distribution; retain adapter compatibility qualification.

## Unreleased historical candidates

- Prepare Runtime 0.8.1 and the paired PostgreSQL recorder 0.2.1 patch candidates.
- Recheck cancellation and absolute deadlines after observer/accounting preparation,
  immediately before dispatch. Count provider requests only at actual attempts.
- Bound JSON and SSE reads by the absolute execution deadline and preserve usage
  already observed before cancellation, timeout or a later preparation failure.

## 0.8.0 — 2026-10-10

- Rename the distribution to `b3-prosaic-runtime` with the index dependency
  `b3-prosaic>=0.4,<0.5`; Python imports and the `prosaic-runtime` CLI stay unchanged.
- Rename the separate PostgreSQL recorder to `b3-prosaic-runtime-postgres` 0.2.0,
  selecting Runtime 0.8.x. Include local Apache license and provenance notices.
- Export source versions and qualify clean Git archive wheels/sdists with matching
  metadata, source payloads, license files and installed import ownership.
- Migrate using a fresh environment or image: legacy and renamed distributions
  share import paths. Candidate artifacts remain unpublished until qualification.

## 0.7.1 — 2026-10-10

- Export validate_execution_artifact, validate_custom_tools and custom_descriptors
  for pure admission without model calls or CLI discovery.
- Reuse execution validation for effort, routing and selected-provider controls,
  including acquisition inheritance of the final agent's execution context.
- Pin Prosaic 0.3.2 with the public canonical artifact-definition validator.

## 0.7.0 — 2026-10-09

- Ship recorder adapter 0.1.1 with read-only catalog/type/constraint/grant readiness
  checks; incompatible schema or missing runtime privileges fail before dispatch.

- Native Anthropic Messages profiles support text, streaming, and Runtime-managed
  builtin, custom, and CLI tools through the shared bounded execution loop.
  Existing profiles continue to default to OpenAI-compatible Chat Completions.
- Anthropic usage capture handles cumulative SSE snapshots and distinct cache
  categories. Cache-write and unsupported pricing categories remain unpriced;
  conflicting or partial evidence cannot produce a trusted cost estimate.
- Anthropic supports `temperature: null` to omit sampling parameters; unsupported
  effort, thinking, JSON mode, and server tools fail explicitly. Durable CLI
  accounting accepts `--accounting-provider anthropic`.

## 0.6.0 — 2026-10-08

- Optional trusted execution context records customer/account, user/project and
  workflow lineage; omitted IDs resolve to configured defaults or `default`.
  Existing unmetered SDK/CLI calls and serialized Result fields remain compatible.
- Per-provider-call accounting captures UTC timestamps, requested/reported model
  and input/output/cache/reasoning token usage across HTTP, SSE and tool loops.
  Unknown/partial evidence is retained; duplicate-key/conflicting usage is quarantined.
- Separate `prosaic-runtime-postgres` 0.1.0 adapter persists intent before dispatch,
  immutable scoped observations and decimal cost estimates with frozen rate cards.
  Replay is idempotent, queries are bounded and reporting uses a consistent snapshot.
- Recorder failure never automatically retries a paid model request. Accounting
  remains opt-in; customer charging, billing export and monetary caps are not enabled.

## 0.5.3 — 2026-10-07

- Pin Python Prosaic 0.3.1 at immutable commit
  `ee145ef29a62dbcf7f5e8abdf1d979749398718d` and refresh installation guidance.

## 0.5.2 — 2026-10-07

- Public `StructuredToolLoop` for explicitly selected JSON tool-request transport:
  closed envelopes, registered-tool authorization, bounded history/deadlines,
  correction and repeated-call detection. Final domain admission remains host-owned;
  native calling remains a separate explicit transport, not an automatic fallback.
- Host-controlled `cli_sandbox_v1`: optional required-mode Seatbelt (macOS) or
  Bubblewrap >= 0.12.0 (Linux, including ARM64), private scratch/HOME, read-only
  evidence/dependencies, no host IP network and fail-closed preflight. Default is
  off; callbacks/validators/native coding providers remain outside this boundary.
- Sandboxed CLI walkthroughs, synthetic containment probe and Linux/macOS CI gates;
  Ubuntu CI grants user namespaces only to its trusted Bubblewrap executable,
  retaining host-wide AppArmor restrictions.
- Retains the immutable Python Prosaic 0.3.0 dependency; no Node/npm required.
- Supports macOS framework Python startup with an exact framework-library read
  grant; no broad interpreter-prefix or workspace access is added.

## 0.5.1 — 2026-10-01

- Pin the Python Prosaic 0.3.0 release, replacing the TypeScript CLI dependency.

## 0.5.0 — 2026-10-01

- Operator-trusted YAML CLI-tool manifests, fixed executable/argv execution
  without a shell, explicit environment grants, read-root validation, bounded
  subprocess output/time, cancellation and offline availability/version preflight.
  Runtime `preflight` checks an agent or all tools without model requests.
- Complete installable CLI analyzer, neutral Markdown, HTTP/SSE runner and optional
  actual Understanding adapter; host Python callback and CLI-tool guides.
- CLI descriptors bind manifest semantics and resolved executable paths; relative
  PATH lookup is canonicalized before changing the tool's working directory.
  Tools remain trusted host code, not an OS sandbox or complete injection defense.
- Release verification: 265 Runtime tests pass, including native HTTP/SSE and
  CLI boundary cases; source distribution and wheel build successfully. The
  earlier development campaign results below are retained as dated observations.

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
