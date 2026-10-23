# Unified pool CLI implementation plan

> **For agentic workers:** Use superpowers:executing-plans inline; test each task before implementation.

**Goal:** Expose the existing unified pool through the installed CLI with explicit authority.
**Architecture:** Project validated CLI flags into PoolPolicy, NativePolicy and RunPolicy; reuse UnifiedRuntime without changing selection or replay rules.
**Tech Stack:** Python 3.11, argparse, pytest, immutable wheel consumers.
**Spec:** ../specs/2026-10-23-unified-pool-design.md and maintainer's CLI extension request.

## Global constraints
Preserve legacy CLI behavior, source Markdown and existing evidence. API budgets do not bound opaque native provider calls. Missing backend policy excludes it.

## Review focus
Missing/zero budgets; ignored grants for disabled backends; profile/accounting incompatibility; JSONL cleanliness; actual installed subprocess behavior and cooldown persistence.

### Task 1: Runtime CLI
- [x] Write tests in tests/test_unified_cli.py for explicit flags, exclusion, counters and output modes; observe failures. Real mixed/native transport checks live in Lab's installed subprocess lane.
- [x] Add unified flag validation in src/prosaic_runtime/cli.py; reject incompatible flags before dispatch.
- [x] Run focused and full Runtime suites; update docs/unified-pools.md and request independent review. 29 CLI tests; full suite 1,204 passed, two existing skips; no material review findings.

### Task 2: Installed consumer qualification
- [x] Add Lab subprocess acceptance using only installed public CLI and owned fixtures; observe failure on old candidate. Final contract: 13 checks.
- [x] Build exact committed Runtime wheel; curate separate manifest/environment; run installed smoke and Lab suite. Lab: 1,504 passed with one existing warning.
- [x] Build consumer image and run offline CLI acceptance; run bounded live backend checks, save filtered evidence, verify cleanup. 33 offline checks; six live scenarios, 7/60 mixed units, zero unknown reservations.
- [x] Record hashes, commands, actual results and limitations in a separate CLI qualification ledger: Lab docs/verification/unified-runtime-cli-2026-10-23.md.

Ruling: Continue inline within the approved CLI extension scope, preserving the existing feature branch and unrelated edits. Native-effect and mixed-fallback CLI tests belong in the installed Lab lane; unit tests cover projection and legacy-mode boundaries. No selection or replay implementation changes were necessary.

Exact executable source: 65385bcac87da084f6fd65ce3dbaf96bafe279c9. Wheel SHA-256: 55329005c70c17539a23ccbde7c94cfca29cf11e77f0a4279f0fb0f056d3d0be. Subsequent documentation records qualification without changing that candidate. Controlled capacity marks validate switching; real provider quota exhaustion was not induced. No publication or push.
