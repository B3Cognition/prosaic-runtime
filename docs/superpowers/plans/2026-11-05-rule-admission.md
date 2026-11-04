# Rule Admission Implementation Plan

**Goal:** Enforce host-registered guards bound by Core's inert enforcement tags in conversation turns, off by default.

**Architecture:** `rules.py` compiles inspection `rules` into a `RuleSet` at turn admission; `rule_hooks.py` runs bound guards at the existing loop's hook points and returns decisions the loop turns into ordinary results; `guards/` ships four generic guards that hosts register explicitly.

**Tech Stack:** Python 3.11+, pytest, existing synthetic HTTP provider fixtures, Prosaic Core 9d5a02f.

**Spec:** ../specs/2026-11-05-rule-admission-design.md (decisions approved 2026-11-05).

## Global Constraints

- No tag, no behaviour: untagged agents stay byte-identical (golden capture from main 63b4d49).
- Hooks never raise into the loop; fix is never success on its own; one retry per turn within existing budgets.
- Rule text, reasons and settings are untrusted data; ids only in `TurnResult`.

## Task 1: Golden capture before any change

- [x] Capture 14 untagged scenarios (2 providers x turn/stream/tool/prepared/budget/run/run-tool) from untouched source, twice, deterministic.
- [x] Mark the fixture as reviewed; regeneration requires `--regenerate-after-review`.

## Task 2: Failing tests

- [x] Headline golden test across four host configurations.
- [x] Admission, compilation, fingerprint, required guards, older Core, threat model, path settings, entry-point refusal.
- [x] Every stage x action on both provider wires; retry budget; fix bounds; guard faults.
- [x] Generic guards and the value_in_tool_output replay fixture. RED observed: collection failed for the missing API.

## Task 3: Implementation

- [x] rules.py, rule_hooks.py, guards/, artifacts/config/conversation/turn_capture/execution/runtime/native integration.
- [x] Ruff check and format on new files only.

## Task 4: Documentation and CI

- [x] docs/rules-admission.md, README paragraph, CHANGELOG Unreleased, terminal-completion cross-reference, MANIFEST entry.
- [x] test.yml Core pin to 9d5a02f; publish.yml and dependency floor left to the maintainer.
- [x] Full suite after review follow-up: 1897 passed, 2 skipped, 2 failed (the two effort-discovery cases that fail on main). Installed-wheel smokes pass; PR description drafted (no push).
