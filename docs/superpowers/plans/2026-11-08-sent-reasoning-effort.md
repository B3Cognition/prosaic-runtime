# Sent reasoning-effort implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans natively, task by task. Watched TDD, no implementer agents, one fresh SDK review and one final consumer review.

**Goal:** Expose exact sent effort controls per transport attempt in Runtime
results and release a matching public-SDK Lab blueprint.

**Architecture:** An invocation-owned bounded collector projects allowlisted
controls from actual HTTP bytes/native argv at existing dispatch boundaries.
Results, pools/adapters and sanitized durable turn receipts retain owned evidence.
No request, budget, authority, input identity or ledger schema changes.

**Tech Stack:** Python3.11–3.13 Runtime; Python3.12 consumer; public Core0.5.0,
Harness0.9.12/recorder0.3.0/store0.4.1; PostgreSQL16/18; original CI artifact promotion.

**Spec:** ../specs/2026-11-08-sent-reasoning-effort-design.md

## Global constraints

- Result extension `reasoning_effort_v1` uses the exact version1 shape from spec.
  At most64 rows/32768 canonical JSON bytes; explicit completeness/count/omission.
- Derive `sent` only from encoded bytes/actual native command, after final
  authority checks and before HTTP handoff, or only after native launch.
- No provider-honored claim, raw request/headers/prompts/environment/paths, new
  inference, retries, budgets, model capabilities, DDL or adapter publication.
- Input/artifact/rule/prepared/workflow identities stay unchanged; private result
  receipts may gain additive metadata. Preserve old receipt absence and snapshots.
- The user selected this documented Cerebro item and explicitly authorized
  implementation/tests/merges/releases; prior native execution/isolation and
  immutable release rules persist. No repeated routine scope/permission question.
- Existing isolated branches start at Runtime3bdcd79 and Laba579039; their exact
  complete post-merge CI was SUCCESS immediately before this task. Use that clean
  baseline, not a duplicate unchanged broad run. New code requires fresh suites.
- Planned Runtime0.9.18/Lab0.9.11 were checked unused; reverify before tagging.
- Preserve all old release/tag/assets, normal dirty user repos, original
  companion worktrees and saved original regression expectations. No live calls.

## Review focus

1. A prepare failure/cancel/capacity skip cannot manufacture sent effort; failure
   after handoff retains it even if observation/serialization fails.
2. A toggle's true value cannot be reported as a transmitted high tier; none and
   omission remain distinct. Actual bytes override configuration/prose assertions.
3. Aggregation retains every fallback/repair/tool attempt; a trusted hook cannot
   overwrite captured trace with fabricated metadata. Unknown stays incomplete.
4. Caller mutation/concurrency and reused runtimes cannot alias/leak trace state;
   arbitrary response fields/control shapes never leak raw content.
5. Small metadata/node/depth caps and long executions truncate evidence explicitly
   without altering guards/accepted outputs/budgets/receipt recovery or identity.

## Task 1: Bounded evidence and API dispatch

**Files:** create src/prosaic_runtime/effort_evidence.py and
 tests/test_sent_effort.py; modify runtime.py, turn_capture.py and direct_tool.py
 only at existing result/dispatch boundaries. Existing accounting Capture stays
 unchanged; no new intent or observation fields.

**Interfaces:** `EffortTrace` owns rows/count/unknown status;
 `http_attempt(data, *, backend, profile, call_id, requested, configured)` returns
 an allowlisted owned row or unavailable row;
 `EffortTrace.add(row)`, `.merge(document, *, unknown_if_absent=True)` and
 `.to_dict(max_bytes=32768)` validate/copy/aggregate evidence. Native row factory
 follows in Task2. Metadata key/capability name is `reasoning_effort_v1`.

- [ ] Capture actual public0.9.17 artifact/rule/prepared input identity goldens in
  an isolated installed environment and verify import ownership before edits.
- [ ] Write both-wire HTTP tests comparing independent actual fixture bodies and
  X-Client-Request-Id with returned rows: run/turn/prepared, artifact/default,
  explicit none, absent controls, automatic/explicit Gemma/Qwen formats,
  streaming, tools/acquisition and guard repair. Watch missing evidence failures.
- [ ] Add pure contract tests for unknown fields/types, requested/default vs sent,
  unknown/binary formats, privacy, copies/concurrency,64row/32768byte bounds and
  complete/count/omission semantics; no general request body copier.
- [ ] Implement pure projection/collector. Allocate per invocation outside backend;
  at final HTTP boundary project actual request bytes/correlation, then perform
  existing final authority/reservation and append before opener. Never double-count.
- [ ] Test failure-before-dispatch (prepare, cancellation, limits, no API key),
  connect/HTTP/read/observe failures and late callback failure; zero vs attempted
  evidence must match actual boundary and budgets. Keep accounting schemas intact.
- [ ] Preserve validated/bounded extension in TurnCapture; trim rows against
  metadata/node/depth cap, then omit only if minimal envelope cannot fit. Test
  successful output/guard/evidence semantics and immutable prepared identities.
- [ ] Model-free direct tool returns zero attempts. Run existing accounting,
  metadata/headers/rules/turn/budget tests plus new cases; commit one tested unit.

## Task 2: Native, pooled and adapter results

**Files:** native_backends.py, native.py, unified.py, adapter.py;
 tests/test_sent_effort_native.py, tests/test_sent_effort_aggregation.py.

**Interfaces:** `native_attempt(argv, *, backend, profile, model, requested,
 configured)` extracts only exact effort argument subset. execute_native adds a
 row only if ProcessOutcome.launched. Every exported runtime advertises capability.
 Native/unified/adapter collectors use Task1 merge/copy/bounds and win over
 delegate/hook-supplied metadata.

- [ ] Write watched native child tests for Claude --effort and Codex -c formats,
  artifact/default/omission, startup refusal/cancel/metadata discovery exclusion,
  capacity skip, safe fallback and failure after launch. Never invoke live CLIs.
- [ ] Derive the safe subset from the actual command passed to run_process,
  retain original requested/profile defaults separately, and collect launched rows.
- [ ] Write watched native→native, API→API, API→native and native→API pool tests;
  compare actual child/HTTP fixtures with all retained rows, preserve earlier
  settings on late failure/cancel, mark uncertain delegate evidence incomplete.
- [ ] Write adapter retry/hook tests: execute twice retains both traces; final hook
  cannot replace them, no execute means zero, changed arguments/allowances retain
  original safety. Add concurrency/copy/aggregate bounds and existing identity goldens.
- [ ] Implement owned aggregation in NativeRuntime/UnifiedRuntime/_Attempts,
  including zero-attempt early Result paths; do not modify fallback authority.
- [ ] Run affected existing native/unified/adapter/accounting/effort suites and
  all new regressions. Commit the independently tested propagation unit.

## Task 3: Durable compatibility and Runtime0.9.18

**Files:** shipped sent-effort tests and adapters/postgres/tests coverage if needed;
 release workflow installed feature gates, version/filename assertions,
 README/accounting/native/pool docs, cerebro-requests/releases and release0.9.18.

- [ ] With actual published Harness0.9.12 and recorder0.3.0/store0.4.1, test
  run/pause/reopen/receipt lost-ack recovery retaining trace with zero redispatch,
  correlation joining existing ledger call IDs, old receipt absence preserved.
  Require actual PG16/18 hosted gates if local engine is unavailable; no skip pass.
- [ ] Update only Runtime version0.9.18, relevant filenames/assertions and
  installed metadata/sent-effort gate; retain original adapter witness versions
  and never republish their local witness bytes.
- [ ] Fresh complete source, clean archive/build/payload and isolated installed
  wheel/smokes/pipcheck; exact identity goldens and compatibility probes.
- [ ] One fresh read-only whole SDK review. Fix watched findings once; no
  per-task agents/re-review loop. Push final-ready PR; require complete exact-head
  ordinary runs and GraphQL overall SUCCESS, including duplicate/cancelled contexts.
- [ ] Expected-head merge/equal tested tree; tag unused0.9.18 at merged source.
  Build/qualify with Core42014531336/source67b3807281ed0cdfd345fff84c56b236ec82cfb1.
  Verify all original CI hashes/required records; promote same bytes once.
- [ ] Anonymous PyPI/GitHub bytes, ordinary public resolver, feature/installed
  compatibility/smokes and merged-source CI must pass before Lab pins advance.

## Task 4: Lab blueprint, released train and publication

**Files:** lab_runner/blueprints/sent_effort/, installed both-wire/CLI tests and
 required PG recovery; registry/package/image/distribution inventories; complete
 family gates, selectors/snapshots/current profiles/freezes; version0.9.11,
 current guides/feature map/release notes/handoff/progress and docs companions.

- [ ] Preserve previous active two manifests, public SDK profile and both memory
  freezes byte-for-byte as pre-sent-effort snapshots, keeping historical selection.
  Add watched complete-train/copy/resource tests; local candidate env is explicit.
- [ ] Copyable no-call list/copy host and guide consume only public installed
  APIs. Show exact sent fields/omission and provider-confirmation limits; native
  defaults/inner CLI calls remain unobserved. No Cerebro app or raw request logger.
- [ ] Both synthetic wires, HTTP streaming/tool/repair/failure, prepared/adapter/
  pool coverage, CLI argument fixtures, file/PG no-call recovery and old receipt
  absence are installed acceptance. Actual PG16/18 fails missing infrastructure;
  separate owned migration/runtime roles, exact cleanup. No local Docker claim.
- [ ] Only after Runtime publication/public proof pin exact public0.9.18
  wheel URL/SHA/source in both active manifests; other4SDK artifacts unchanged.
  Refresh only current provenance/freezes, preserving corpus/prompts/scoring and
  every previous blueprint resource, golden and historical evidence.
- [ ] Fresh full source/frontend/browser/native/reasoning/rules/features and
  wheel/rebuilt-source/public-index/payload checks, actual counts only. One fresh
  whole consumer review, watched fixes, final-ready PR and all ten final-head jobs.
- [ ] Verify original CI ZIPs, actual checkouts/counts and source payloads,
  expected-head merge/equal trees, unused tag and publication of original qualified
  CI archives with safe proof. Anonymous downloads/public consumers and independent
  post-merge matrix/proof; never promote post-merge rebuilds over qualified bytes.
- [ ] Update all bits' current READMEs/release notes and mark Cerebro item4 done
  with limits; remaining requests stay documented. Related docs-only PRs merge
  after public Lab proof, all current mains overall SUCCESS. No unrelated releases.
- [ ] Archive own execution ledger/rulings/reviews before deleting only this own
  skill workspace. Preserve normal user repos/worktrees and all prior assets.

## Self-review

Spec fields/bounds and each review-focus condition map to Tasks1–2 regressions;
durability/identity/installed release is Task3, public consumer/release is Task4.
No new standalone execution method or raw-backend public API is introduced.
The prior explicit native workflow and selected documented feature supply scope;
routine names/bounds/transport projection choices are builder calls in the spec.
