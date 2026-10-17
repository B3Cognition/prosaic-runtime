# Tool finalization implementation plan

> **For agentic workers:** Use superpowers:executing-plans for the already selected native execution. Steps use checkbox syntax for tracking.

**Goal:** Make the next provider turn final-answer-only after an explicit host-configured number of tool rounds, without changing defaults or budgets.

**Architecture:** Shared ExecutionBackend owns the phase transition; adapters retain existing request encoding. Endpoint features carry the opt-in finalize_after_tool_rounds integer. Remove tools and add trusted final-answer guidance after the configured round; reject noncompliant tool requests before dispatch with a distinct code. Lab qualifies an immutable candidate wheel through installed public APIs.

**Tech Stack:** Python 3.11+, existing pytest/HTTP fixtures, PEP 517 wheels and checksummed SDK manifest.

**Spec:** Maintainer-approved Runtime finalization proposal in the Integration Lab agent-quality worktree; user explicitly authorized extending Runtime and qualifying its pinned wheel.

## Global constraints

- Defaults, serialization, grants, budgets, cancellation and recovery authority stay compatible.
- Model/prose cannot change the host feature; final-only requests cannot dispatch handlers.
- Preserve main/example changes; isolated base is qualified v0.9.3 commit 342d4fa.
- Local candidate version 0.9.3+finalize.2; no publication, tag or merge.
- Preserve all prior unknown reservations. Residual live ceilings: 57 requests /149 tools /604876 tokens.
- Checksummed candidate wheel and explicit provenance; no editable SDK or installed-file patch.

## Review focus

1. Provider ignores finalization: block new handler and retain usage.
2. No-progress/acquisition: defaults and failure codes remain accurate.
3. Streaming/Anthropic: final payload has no tool declarations or forced-tool choice.
4. Invalid or excessive threshold: reject before HTTP dispatch.
5. Budget/cancellation: no extra request after exhaustion, no fabricated usage.

## Task 1: Runtime finalization

Files: new tests/test_finalization.py; modify execution.py, README.md and candidate versions in pyproject.toml / __init__.py.

- [x] Record clean released-base full-suite result.
- [x] Write HTTP regressions for thresholds one/two, final-only payload, hostile provider, defaults, invalid values, cancellation and exhausted budgets. Observe failures before implementation.
- [x] Validate feature at loop entry, track explicit finalization separately from no-progress, add trusted phase guidance once, remove tools on final request and retain counts/admission.
- [x] Focused and full Runtime suites, one fresh final review, regression fixes for substantiated findings.
- [x] Document behavior; build wheel/sdist and verify installed ownership/smoke/hashes.

## Task 2: Candidate consumer qualification

Files: explicit candidate manifest/provenance/configuration and dated verification record. Any additive local-version report admission gets a failing unit test; manifest matching remains strict.

- [x] Replace only Runtime in a checksummed candidate manifest; retain four released sibling wheels. Install separately and audit payloads.
- [x] Candidate benchmark package binds that provenance. Run ten fixture cases and three recovery probes via installed SDKs; verify final-only wire and unchanged grading.
- [x] Full Lab source suite for any consumer-code change. Preserve original release manifest/evidence.
- [x] If residual ceilings permit, fresh candidate study of ten cases × one repetition × two profiles, 300-second cases, within exact residual capacity. Label smaller schedule; do not merge with the three-repetition baseline.
- [x] Offline reconciliation/replay; record outcomes, source/wheel identity and limitations. Preserve worktrees; no release.
