# Tool finalization progress

Plan: docs/superpowers/plans/2026-10-16-tool-finalization.md.
User authorized Runtime extension and pinned-wheel qualification; native execution persists.
Ruling: native chat worktree tool targets Core, not sibling Runtime; manual worktree /private/tmp/prosaic-runtime-tool-finalization, branch codex/runtime-tool-finalization, base v0.9.3 — preserves unrelated main/example/context assembly work — cost: manual lifecycle.
Ruling: use local version 0.9.3+finalize.1 because v0.9.4 exists on unrelated work — distinguish candidate from release — cost: additive local-version report admission may be needed, retaining strict manifest matching.
Ruling: current approval covers the concrete proposal and implementation; no repeated planning approval gate — choices stay within the approved transition and qualification scope — cost: routine implementation choices remain agent decisions.
Baseline suite running; no implementation written yet.
Initial base run: 1078 passed, 2 skipped, 363.63s. Collected/loaded before implementation; later CLI subprocesses can see working-tree edits, so the final fresh whole-suite run is the authoritative candidate result.
Regression cycle: initial 16 failures/2 passes; two budget-code assertions corrected to existing provider_request_limit/token_limit semantics. Full feature-withdrawal witness 20 feature regressions fail; five baseline guards preserve prior behavior after correcting tool_started (attempt) versus successful tool_completed (execution) accounting. Shared-loop implementation is opt-in, separate from no-progress, uses host system guidance and removes final request tools.
Candidate local version updated, including distribution/conformance expectations. Preliminary finalize.1 wheel passed clean installed smoke, metadata gate and five-wheel audit. Lab source suite: 1034 passed, one existing Starlette warning, 116.21s.
Final review: one Important ordering defect substantiated. Public-turn regression on both transports fails with invalidToolRequest before fix; explicit-finalization guard moved ahead of batch validation. Public turns retain closed providerFailure mapping. Fixture initially recreated Runtime and lost configuration; corrected before valid RED witness.
Final review ruling: tool-free OpenAI invokes no tool phase; feature validation remains at shared tool-loop entry as specified. Cost: irrelevant invalid feature values in tool-free calls are ignored.
First fresh candidate suite: 1102 passed, two expected skips, one clean-archive version failure because HEAD was still released base. Save local candidate commit before authoritative rerun. Revised candidate finalize.2 preserves preliminary finalize.1 archive and pin.
