# Generic token admission and honest accounting

Approved scope: fix the general budget gap before another live comparison. No
live calls, releases, model changes, or allowance increases are authorized here.

## Contract

Runtime's existing reported-token cap is a post-response stop threshold. An
opt-in host ExecutionBudget with a trusted TokenCounterRegistration adds request
admission. Count the final native input and reserve it plus its positive
max_tokens output bound immediately before HTTP dispatch. Validate the counter's
provider/model binding. Check every enclosing budget and the invocation's token
remainder before any debit. Keep in-flight reservations under ordered shared
locks so concurrent and nested execution cannot spend the same remainder.

Complete usage replaces the reservation with the actual reported amount.
Unknown outcomes retain the reservation and block further dispatch. A provider
report above the admitted bound remains fully charged and permanently blocks
new dispatch, even if the overall ceiling has room. Trusted counter guarantees
and provider conformance are assumptions, not a claim about provider billing.
No tokenizer, provider-specific approximation, or model prompt is bundled.

Lab adds an explicit v2 ledger for new runs. It persists known overruns, including
amounts above aggregate ceilings, and prevents new reservations. Historical v1
ledgers remain readable and retain their old validation and bytes. Reports take
uncertainty and breach state from the durable claim rather than stale evidence
flags. Recovery never reruns inference or refunds unknown reservations.

New live benchmark entry points require a host-supplied trusted counter and a
Runtime with admission support; absence fails before dispatch. Fixture-only
counters must never be presented as qualified live counters. Harness continues
to orchestrate through its public APIs; host budget activation spans its retries.

## Execution plan

1. Runtime: watched failing tests for admission, exact fits, larger continuations,
   concurrency, nesting, local caps, unknown outcomes and truthful overrun.
   Implement the public opt-in ExecutionBudget contract. Run targeted/full suite.
2. Lab: watched failing tests for v2 overrun persistence, reopening, idempotence,
   concurrent claims, v1 compatibility, and durable-state reporting. Wire strict
   host admission into execution and live preflight, without raising limits.
3. Build immutable Runtime/Lab candidates and qualify the installed public SDK
   boundary using synthetic providers. Run full suites, a fresh whole-change
   review, and an offline replay with network dispatch denied.

## Progress

- Design: fixed above. Existing isolated Runtime and Lab worktrees reused on
  codex/budget-admission and codex/budget-accounting. No Core changes.
- Pre-flight: Runtime's public ExecutionBudget is consumed by Lab; Lab must use
  the candidate installed wheel, never private imports or source monkeypatches.
- Ruling: strict admission is opt-in for general Runtime consumers and required
  for new live Lab benchmarks. Existing stop-threshold behavior remains valid
  for older callers; missing trusted counters cannot silently become estimates.
- Runtime regressions: initial 10 tests failed for missing token_counter API;
  admission/continuation/transport/concurrency cases passed after implementation.
  Extended Anthropic, streaming and adapter tests passed (16 total).
- Review: one fresh reviewer found below-sample request breaches lost by Lab and
  complete captured usage lost on recorder failure. Both reproduced before fixes.
  Capture/budget regression set: 48 passed after the fix. No live calls.
- Qualification environment: initial source suite used an environment without
  setuptools; build gates failed. A new isolated test environment fixes this.
  A concurrent-suite CLI output-limit failure passed all 31 CLI tests on rerun;
  final full suite is still required after the reviewed source is committed.
