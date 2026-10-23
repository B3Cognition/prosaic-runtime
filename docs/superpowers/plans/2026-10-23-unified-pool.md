# Unified Pool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Select Claude, Codex and API/local profiles through one bounded pool with safe pre-work switching.

**Architecture:** An opt-in facade delegates to existing native/API engines. A transport-owned API pre-work proof complements native safe-fallback evidence; shared capacity and separate budget counters govern selection.

**Tech Stack:** Python 3.11+, dataclasses, existing Runtime and POSIX CapacityStore, pytest, installed-wheel Lab consumer.

**Spec:** `docs/superpowers/specs/2026-10-23-unified-pool-design.md`

## Global Constraints

- Existing native/API execution, configuration formats and guarantees remain compatible.
- API request/tool caps must be explicit; native internal requests/tools are opaque.
- Switch only when work provably has not started; observer events never establish retry authority.
- No mid-task migration, implicit live probes, subscription exhaustion or server startup.
- Lab uses installed immutable wheels/public APIs; preserve historical reports and released manifests.
- Existing checkout/feature branch `codex/native-profile-pools` is reused, consistent with the prior approved native-extension plan. Preserve unrelated example changes. No publication.

## Review Focus

- An initial tool or a successful earlier response prevents later 429 fallback.
- Shared quota aliases across backend families cannot bypass cooldown.
- Missing policy, required capabilities and exhausted counters never broaden authority.
- Callback/transport ambiguity preserves failure and never dispatches another candidate.
- Unknown native usage never becomes zero or a strict API token/request guarantee.

---

### Task 1: Configuration, policies and API pre-work proof

**Files:** Create `src/prosaic_runtime/unified_config.py`, `tests/test_unified_config.py`; modify `src/prosaic_runtime/runtime.py`; add `tests/test_prework_safety.py`.

**Interfaces:** `UnifiedRuntimeConfig(native_config, api_config, pools, default_pool, api_quota_keys, capacity_file=None)` and `.load(path)`; `PoolPolicy(timeout_s, max_attempts, max_native_attempts, native_policy=None, api_policy=None, required_capabilities=frozenset())`; API Result metadata `prework_failure_v1` includes only transport-owned safe capacity proof.

- [x] Write and observe failing config/policy tests for missing explicit caps, collisions, references, capabilities and invalid bounds.
- [x] Implement strict dataclasses/loading with source-relative config paths; preserve both existing loaders.
- [x] Write and observe failing real-HTTP safety tests: initial 429/refusal safe, 401/503/timeout unsafe, tool or previous response then rejection unsafe.
- [x] Implement minimal transport-owned safety evidence, excluding budget/cancel/callback failures.
- [x] Run focused tests and existing native/API safety regressions.

### Task 2: Unified Runtime delegation and documentation

**Files:** Create `src/prosaic_runtime/unified.py`, `tests/test_unified_runtime.py`, `docs/unified-pools.md`, `examples/unified.yml`; modify `src/prosaic_runtime/__init__.py`, `CHANGELOG.md`.

**Interfaces:** `UnifiedRuntime(config, *, source='.prosaic', executable='prosaic', capacity=None).run(artifact, arguments='', *, cwd='.', policy, pool=None, on_event=None, cancelled=None, env=None) -> Result`. Capability `unified_pools_v1`; attempt metadata `unified_execution_v1`.

- [x] Write and observe failing mixed CLI/HTTP tests with actual subprocesses/loopback servers: both switch directions, persistent cooldown/all-unavailable, shared quotas, permissions/capabilities, counters/deadline/cancellation, unknown usage and no replay after work.
- [x] Implement ordered selection through existing engines; no copied execution loop or observer-derived retry decisions.
- [x] Reduce API/global time allowances, retain structural attempt evidence and preserve uncertain failures.
- [x] Document explicit Python policy/config usage, limits and compatibility. CLI support is a separate future surface; current CLI must fail clearly for kind unified.
- [x] Run full Runtime suite, build wheel/sdist and installed-wheel qualification; independent review before final completion.

### Task 3: Installed-candidate Integration Lab qualification

**Files:** Integration Lab `lab_runner/native_eval/unified.py`, tests under `tests/native_acceptance`, documentation/evidence ledgers. Private candidate manifest/config/results remain ignored.

**Interfaces:** Public installed `UnifiedRuntime` family, existing Lab shared allowance/provenance audits and neutral repair artifact.

- [x] Build from an immutable Runtime commit containing only this task's changes; audit and install into a separate SDK-only Lab environment/candidate manifest.
- [x] Write and observe failing acceptance tests, then add a host-owned mixed fixture lane covering failover, unsafe no-replay, cooldowns, all-unavailable and separate budgets.
- [x] Run Lab tests, immutable consumer build and exact-owned network-disabled fixture container with verified cleanup.
- [x] Run bounded live selection of real Claude, Codex and local using the same Markdown/inputs and explicit per-backend policies. Controlled cooldowns do not pretend to be real quota rejection. Preserve unknown reservations and all failures.
- [x] Record exact results, SDK hashes, limitations and cleanup; perform independent review and resolve important findings with RED/GREEN regressions.

## Execution ledger

User approval covers the already-described pre-work-only unified selector and explicit policies/budgets; implement inline without another approval round. Implementation/testing steps will be recorded here. No goal, release or publishing operation is authorized.

Baseline: 1129 passed, 2 skipped in 378.08 s. Task 1 RED: 15 missing-contract failures;
task 2 RED: 17 missing-runtime failures. First focused pass: 90 passed. CLI guard
and native policy-mismatch regressions were separately observed RED then GREEN.
Independent review found accepted-response callback errors could counterfeit a
transport rejection. Real HTTP JSON and SSE reproductions observed both 429-shaped
and connection-refusal-shaped callbacks RED before restricting proof to failed
direct transport acceptance; wrapped accounting paths conservatively withhold it.
Ruling: unified native fallback is stricter than the standalone native effect-free
rule: any model text/reasoning prevents switching. Two regressions observed RED
before work-start evidence was added; this avoids replaying partial model work.
Existing malformed-stream regression caught a missing type guard during this fix;
the invalid block is again rejected without raising an AttributeError.
The pre-review full run passed 1169 tests, 2 skips, in 388.95 s. Final source
passed 1175 tests, 2 existing skips, in 385.27 s; focused final native/unified
coverage passed 97. The immutable implementation commit is
`ecb3b4cdd1522c537c5acb9685bf39ff78bbbd1d`; wheel SHA-256 is
`5496ebba6ab3559cf143198dcb8c44aab8fea7051d3f111af309c0795d4cd381`.
Installed base/native/conversation/production wheel acceptance passed.

Lab qualification: 11 candidate acceptance tests, full shared checkout 1477 tests
(one existing Starlette warning), and exact-image offline acceptance 11 unified
plus 9 existing native checks. Six live controlled-capacity scenarios passed:
Claude, Codex, local, all unavailable, before expiry and restored Claude.
Four real assignments completed; both negative capacity checks dispatched nothing.
Durable allowance retained 7/60 known mixed units (3 native + 4 local requests),
zero unknown. Independent Lab review verified outputs, hashes, controls and
accounting. Owned local server/container/workspace cleanup is verified.
See Integration Lab `docs/verification/unified-runtime-pool-2026-10-23.md` for
complete receipts, setup failures and qualification limits. Real provider quota
exhaustion was not induced; local-source candidate is not a release.
