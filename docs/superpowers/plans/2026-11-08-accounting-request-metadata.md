# Accounting request metadata implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan natively, task by task. Use watched TDD and one fresh whole-branch final review, not per-task implementer agents.

**Goal:** Make trusted run/variant labels available to recorder `prepare(intent)`
before each accounted provider request, then release Runtime and a Lab blueprint.

**Architecture:** Optional immutable `ExecutionContext.request_metadata` resolves
and freezes into a versioned optional context extension. Existing Capture already
serializes resolved context into every intent, so no provider/request-body engine
changes are needed. Existing published PostgreSQL0.3.0 and Harness0.9.12 consume
Runtime's public context decoder/child API; verify that compatibility explicitly.

**Tech Stack:** Python3.11–3.13 SDK; Python3.12 consumer; pytest; public installed
Core0.5.0/Harness0.9.12; PostgreSQL16/18; exact qualified CI artifact promotion.

**Spec:** ../specs/2026-11-08-accounting-request-metadata-design.md

## Global constraints

- At most16 labels; ASCII keys `[A-Za-z][A-Za-z0-9_.-]{0,63}`; printable nonempty
  values at most256codepoints/256UTF8bytes; existing canonical JSON at most4096bytes.
- Reserved keys and exact versioned serialized shape are fixed by the spec.
- None/empty means absence and preserves exact old context/intent bytes and hashes.
- Attribution-only defaults; metadata explicitly supplied, frozen by construction
  and child/resume, never authority. Saved Harness labels win; new variant needs
  new run. No provider payload/header/prompt propagation or raw request capture.
- Published recorder0.3.0 code/DDL/privileges/reports and old records remain intact.
  Current source adapter __init__/schema bytes already match the actual published
  wheel5f29ce5b0a1cb3ce4ee41f44c50e322b5b1af73cbe186ecadc428f81ccc7b5ca.
- Runtime0.9.17/Lab0.9.10 tags/releases verified unused. Recheck before tagging.
- Preserve all old release bytes, golden fixtures, saved original regression tests,
  normal dirty user repos and original companion worktrees. No live inference.
- SDK source baseline40e39eb just passed its complete13-job post-merge run
  42426984871; design/plan edits are documentation-only. Do not duplicate an
  unchanged broad baseline merely for bookkeeping. Capture old public0.9.16 goldens.
- User explicitly authorized implementation/tests/merges/releases; no additional
  routine permission/scope gate. Run native execution and ledger deviations.

## Review focus

1. Caller/default/recorder mutation must not relabel later calls or frozen runs.
   Cover original map mutation, read-only field, returned-copy mutation and child.
2. Additive context schema must accept legacy persisted bytes without rewriting,
   reject malformed/unknown extensions and preserve absent/empty hash goldens.
3. Metadata may never become auth/rate/filter authority or reach provider bodies,
   prompts, headers, artifact/prepared/workflow identity. Use unique wire sentinels.
4. Prepare/observe failures, tool/repair continuations and saved receipt adoption
   retain their existing budget, unknown-effect and no-redispatch semantics.
5. Actual published recorder and Harness compatibility, old SDK family selection,
   installed wheel/source ownership and required PG cleanup must be proved.

## Task1: Context contract and common capture behavior

**Files:** create src/prosaic_runtime/accounting_metadata.py and
 tests/test_accounting_request_metadata.py; modify src/prosaic_runtime/accounting.py
 and src/prosaic_runtime/runtime.py (capability only). Reuse existing both-wire
 fixture helpers from test_runtime/test_anthropic/test_rules_execution.

**Interfaces:** consume public ExecutionContext/ResolvedContext and existing
 canonical/fingerprint. Produce immutable normalized metadata, optional
 serialized `{version:1,labels:{...}}`, backward decoder and public capability
 `accounting_request_metadata_v1`. No new run/turn kwargs, Capture or transport path.

- [ ] Capture constant legacy context and canonical/hash using actual installed
  public Runtime0.9.16, fixed request/invocation IDs, and assert import ownership.
- [ ] Write tests before implementation. Core assertions:
  `ExecutionContext(request_metadata={'variant':'baseline'}).resolve()` owns a
  read-only map; `to_dict()['request_metadata']=={'version':1,'labels':{'variant':'baseline'}}`;
  original/to_dict mutations do not change it; `child(invocation_id='child')`
  keeps labels. None/{} output equals captured legacy bytes/hash exactly.
- [ ] Parameterize bounds/reserved keys/custom duplicate items/Unicode/control/
  malformed persisted version, shapes and empty extension; defaults do not inherit
  metadata; exceptions contain no invalid values. Watch missing API failures.
- [ ] Write run/turn/prepared tests on both wires: custom prepare callback sees
  labels while independent request list is empty; intents/results contain the
  extension; unique label sentinels absent from raw provider requests. Tool-loop,
  acquisition and guarded repair requests retain one frozen label map. Watch RED.
- [ ] Implement snapshot/encode/decode helpers and dataclass normalization,
  optional omission/backward schema, resolution/child retention and capability.
  Reuse common Capture unchanged; no ad hoc provider forks.
- [ ] Run focused metadata +existing accounting/context/request-header/rule tests;
  compare failures against implementation/spec, not weakened saved expectations.
- [ ] Commit the independently testable context/capture feature.

## Task2: Durable recorder and Harness compatibility

**Files:** add adapters/postgres/tests/test_request_metadata.py and public installed
 compatibility probes under review/ (untracked private probes may be copied into
 final shipped tests if appropriate); modify CI qualification commands only to
 run the new shipped metadata cases from source and installed wheel.

**Interfaces:** consume Task1 public context extension. Existing adapter0.3.0
 must accept/persist through `prepare`, `observe`, `calls` and report without new
 columns or rewritten old records. Existing Harness0.9.12 must retain labels via
 `ResolvedContext.child` and saved-context resume without a code overlay.

- [ ] Add exact-owned PG fixtures/cases for labelled intent persistence/reopen,
  same-intent idempotence, changed-label conflict, mixed labelled/unlabelled rows,
  unchanged scope/rates/assessment and operator-only calls view. Missing required
  CI DSNs fail rather than count as passing; cleanup names/roles exact-owned.
- [ ] In isolated source SDK env install actual public Harness0.9.12 and recorder
  0.3.0 by verified wheel SHA (no sibling editables). Assert module ownership.
- [ ] Add file-store/public Harness run/pause/reopen/resume tests on both wires,
  labels present before every dispatch, child IDs/attribution retained, changed
  defaults or new observational labels do not relabel saved context. No extra
  request after receipt recovery. Exercise all guarded/untagged identity goldens.
- [ ] Prepare/observe failures and invalid metadata must stop before further
  dispatch with existing accounting_failed behavior and unchanged billing rules.
- [ ] Run source/installed compatibility probes and all relevant existing PG/unit
  tests; actual PG16/18 remains required hosted if local engine unavailable.
- [ ] Commit coverage/qualification changes. Do not republish unchanged adapters.

## Task3: Runtime qualification, merge and immutable publication

**Files:** pyproject.toml, src/prosaic_runtime/__init__.py, version assertions,
 .github/workflows/test.yml/publish.yml filenames, README.md, docs/accounting.md,
 docs/cerebro-requests.md, docs/releases.md, docs/release-0.9.17.md and release plan.

- [ ] Bump only Runtime0.9.17; retain recorder0.3.0 witness and original public
  adapter bytes. Update relevant metadata/filename assertions and installed gate
  to run shipped request-metadata cases. Add identity/schema/resume/limits notes.
- [ ] Run fresh final complete source, build/payload/metadata/pipcheck and isolated
  installed wheel/compatibility tests. No duplicate completed broad reruns absent
  changes/failure. Record actual counts and architectures.
- [ ] One fresh read-only SDK whole-branch review; watched fixes in one pass and
  final affected/full suites. Archive ledger before later workspace cleanup.
- [ ] Push one final-ready PR, require both ordinary exact-head runs and overall
  statusCheckRollup SUCCESS, including same-head duplicates. Expected-head merge,
  fetch and verify equal tested/merged trees; never dismiss reviews/resolve threads.
- [ ] Tag unused v0.9.17 at exact merged source. Qualify Publish SDK using Core
  run42014531336/source67b3807281ed0cdfd345fff84c56b236ec82cfb1. Verify24required
  records, payloads/upstream receipts and original CI artifact hashes.
- [ ] Dispatch publish=true once with successful candidate_run_id on same tag.
  Promote original qualified bytes, never local builds or adapter witnesses.
  Inspect actual failures; strictly verify bytes after propagation before bounded
  failed-job recovery. Verify PyPI/GitHub anonymous bytes/fresh ordinary resolver,
  installed feature gates and all post-merge checks.

## Task4: Lab blueprint, actual released train and publication

**Files:** create lab_runner/blueprints/accounting_metadata/, installed feature
 tests, required PG recovery tests and docs/accounting-request-metadata.md;
 update registry/package/image inventories, exact complete family gates, active
 manifests/public provenance/freezes, selectors/old snapshots and current guides.
 New Lab docs/release-0.9.10.md, handoff/progress, source/front-end version labels.

- [ ] Preserve Lab0.9.9/mainfdb37b5 and all old source/wheel/tag/assets. Snapshot
  both active manifests, sdk-provenance-public.json and memory freezes verbatim
  as pre-accounting-metadata. Preserve historical selectors and corpus/scoring.
- [ ] Add copyable public-API blueprint with no-call list/copy/admission and
  explicit endpoint opt-in; trusted host context creates run/variant labels;
  custom recorder prepare labels before dispatch, no raw request disclosure.
  Keep all five earlier blueprint resource bytes unchanged.
- [ ] Watch missing capability/train/resource tests fail. Use a separately labeled
  candidate-wheel env for development; never call a local SDK candidate released.
- [ ] Installed both-wire acceptance covers run/turn/prepared usage, deep snapshot,
  provider body/header isolation, failure-before-dispatch, loop/repair labels,
  stable empty identities and frozen Harness saved-context/receipt resume.
- [ ] Add real PG16/18 persistence/reopen/conflict and no-extra-call recovery with
  published recorder0.3.0 and store0.4.1, exact-owned cleanup. Do not patch SDKs.
- [ ] Only after actual Runtime publication/public-byte verification advance both
  active manifests to exact0.9.17 public URL/SHA/source. Preserve other4artifacts;
  public profile and active freezes advance without prompts/corpus/scoring changes.
- [ ] Prove exact family admission, refusal of partial upgrades, old profiles and
  all immutable snapshots. Refresh active freezes only after final pins.
- [ ] Final full source, frontend/browser, installed rule/features/native controls,
  wheel/rebuilt-source/public-index/payload/cleanup checks. Actual counts only.
- [ ] One fresh whole-branch consumer review; watched fixes, final-ready PR and
  all ten four-architecture/PG/reasoning jobs, exact-head overall SUCCESS.
- [ ] Verify original CI ZIPs/job checkout trees/counts/payloads, expected-head
  merge/equal trees, unused tag and publish only original qualified GitHub archives
  with sanitized evidence. Anonymous bytes, public wheel/source consumers and
  independent post-merge CI/proof; no local stack or live-provider claims.
- [ ] Update related Core/Harness README/current compatibility notes and retain
  remaining Cerebro backlog. Qualify docs-only PRs in parallel, merge only after
  actual Lab/public-consumer proof, require all current mains overallSUCCESS.
  No Core/Harness API/version/tag changes unless an actual scoped defect requires it.
