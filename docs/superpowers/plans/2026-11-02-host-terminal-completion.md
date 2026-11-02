# Host Terminal Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Complete an authorized native tool loop from a real host artifact without an acknowledgement model call, and persist/validate it safely in Harness.

**Architecture:** Runtime owns an immutable typed output/artifact and execution binding. Harness retains its receipt-before-validation sequence and verifies terminal provenance before accepting output. RuntimeAdapter latches completion to prohibit repeat dispatch.

**Tech Stack:** Python 3.11+, pytest, existing synthetic HTTP fixtures, existing SDK build/release workflows.

**Spec:** ../specs/2026-11-02-host-terminal-completion-design.md (user approved 2026-11-02).

## Global Constraints

- Core Prosaic and PostgreSQL adapter releases remain unchanged; preserve unrelated work.
- Checkpoint version 2 and receipt version 1 remain compatible; new disabled defaults do not change fingerprints.
- TerminalCompletion(output, *, artifact), CustomTool(terminal=False), RunPolicy.allowed_terminal_tools and WorkflowPolicy.allowed_terminal_tools are host authority.
- Inline artifacts only; singleton terminal batches; no journaled terminal tools or unsupported entry points.
- Cancellation, unknown effects and limits never imply success; no automatic replay after terminal completion.
- The user performs product integration/testing after released SDK qualification.

## Review Focus

- Callback mutation must not alter admitted output/artifact or the next validator's context.
- A valid model acknowledgement without typed completion cannot bypass artifact admission.
- Cancellation after a handler returns must preserve known completion while blocking workflow success.
- Adapter retry/rewrite and narrowed grants cannot widen terminal authority or repeat effects.
- Receipt-limit fallback must preserve actual usage and distinguish a breadcrumb from a stored artifact.

## Task 1: Runtime terminal contract and native execution

**Files:** create src/prosaic_runtime/terminal.py and tests/test_terminal_completion.py; modify tools.py, policy.py, tool_registry.py, execution.py, runtime.py, __init__.py and unsupported entry-point admission.

**Interfaces:** TerminalCompletion(output, *, artifact) exposes owned snapshots. terminal.py owns canonical digests, admission, evidence validation and bounded breadcrumbs. New Result.metadata['terminalCompletion'] contains closed version-1 provenance and artifact; critical tool event contains its binding only. terminal_tool_v1 advertises support.

- [ ] Write and run failing provider-fixture tests for typed completion/no extra model call, exact usage, spoofing, grants, batches, cancellation, JSON limits and unsupported entry points.
- [ ] Implement the typed contract, capture-before-cancellation and native loop termination for both provider wires.
- [ ] Run targeted tests plus existing tool/conversation suites; record observed RED/GREEN.
- [ ] Commit Runtime contract/execution changes.

## Task 2: Adapter completion latch

**Files:** modify src/prosaic_runtime/adapter.py; extend tests/test_terminal_completion.py.

**Interfaces:** existing RuntimeAdapter.execute preserves and narrows terminal grants; the private attempt ledger refuses dispatch after terminal evidence. Returned output/provenance must match the actual delegate.

- [ ] Write and run failing adapter redispatch, output-rewrite, callback-failure and grant-reduction tests.
- [ ] Implement ledger latch and authoritative evidence retention; verify all adapter tests.
- [ ] Commit adapter changes.

## Task 3: Harness admission, durable evidence and validation

**Files (Harness repository):** modify factory.py, engine.py and validation.py; create terminal.py and tests/test_terminal_completion.py.

**Interfaces:** WorkflowPolicy.allowed_terminal_tools defaults empty; CheckContext.terminal_completion defaults None and contains owned current artifact/provenance. Harness validates Runtime terminalCompletion metadata against effective host policy, actual tool events, invocation and content digests on acceptance, ledger reads and recovery. Public recovery_evidence remains artifact retrieval.

- [ ] Write/run failing synthetic HTTP workflow tests for acknowledgement plus artifact, schema/semantic rejection without retry, missing/spoofed bindings, limits and restart adoption.
- [ ] Implement pure admission/fingerprints, policy propagation, terminal validator context and no-retry acceptance.
- [ ] Preserve bounded usage/counters and explicit incomplete-artifact breadcrumbs in failed receipts.
- [ ] Verify context isolation, accepted-only retrieval, capability/version/binding mismatch, cancellation and pre-receipt uncertainty.
- [ ] Run targeted Harness tests with the new Runtime; commit Harness implementation.

## Task 4: Complete qualification and consumer example

**Files:** SDK documentation, changelogs, public-API consumer example and wheel-smoke scripts in each repository.

- [ ] Run full Runtime and Harness suites; diagnose any baseline failures before attributing them to this feature.
- [ ] Build SDKs and run fresh installed-wheel smoke, including native terminal completion and restart without repeated effects.
- [ ] Dispatch one independent whole-change review; fix important findings with observed regression tests and rerun affected/full suites.
- [ ] Record exact commands/results and remaining platform limitations in release evidence.

## Task 5: Prepare and qualify releases

**Files:** pyproject.toml, __init__.py, release workflows/pins and release notes in both repositories.

- [ ] Recheck reserved/published versions; expected Runtime 0.9.11 and Harness 0.9.6. Require new Runtime from new Harness.
- [ ] Commit final candidates, build immutable artifacts and run the existing platform/dependency-floor/PostgreSQL compatibility workflows.
- [ ] Promote only successful exact-byte candidates through existing release workflows; never overwrite older tags/assets.
- [ ] Deliver release/install pins, checksums, test evidence and the integration example; distinguish published artifacts from any blocked publication.

## Execution ledger

User explicitly approved implementation/tests/release preparation; no additional design/plan approval gate. Execute inline. Dependency chain: Task 1 evidence/API -> Task 2 adapter preservation -> Task 3 Harness validation -> Tasks 4/5 qualification. No conflicting interfaces found in preflight. The managed worktrees are outside shell writable roots; use reviewed escalated commands for environment/tests/builds, ordinary apply_patch for edits.
