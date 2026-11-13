# Runtime 0.9.25 — guarded-turn recovery

A `fix` guard that reports a violation with `fixed=None` now takes the rule retry path. The retry may spend another provider request within the existing deadline, request and token budgets. Hosts that need a violation to stop immediately can bind the rule with `on_violation=fail`. Guard exceptions and unusable repairs still fault the turn. A before-tool rewrite refused by schema validation is reported in `rules_faulted`.

Hosts can enable `ProsaicRuntime(..., correct_malformed_tool_calls=True)` to give a rejected tool batch one bounded correction request. A host-required first tool stays forced, and the correction asks for a valid resend. A text-only answer fails while that requirement is unmet. Optional tools still allow an answer without a lookup. Once tools have shut down for lack of progress, a malformed batch ends the turn without another request.

Set `rule_retries` from 0 to 3 to control the allowance shared by rule retries, declined repairs and citation repairs. The default is one. `TurnResult` reports `rules_declined`, `rules_retried`, `rule_retries_used` and `tool_request_corrections`.

## Compatibility and identity

Artifact digests and default prepared-turn bytes are unchanged. Enabling tool correction or selecting a retry allowance other than one changes prepared-turn identity. Existing frozen turns retain their bytes and can exhibit the new declined-repair behavior when executed with this release. Core 0.5.0 is the qualification baseline, and the published PostgreSQL recorder remains at 0.3.0.

## Validation

The PR review ran 186 targeted tests on the implementation, covering both provider wires. In nine paired local Nemotron cases with seeded failures, recovery produced valid answers in three of three declined repairs and two of two cases with a two-retry allowance. The three optional malformed lookups and one required malformed lookup produced zero grounded answers. These fixtures measure recovery from seeded failures.

Nemotron grammar and grounded lookup acceptance remain follow-up work. Harness forwarding, resume identity and recovery receipts are tracked in [issue #37](https://github.com/B3Cognition/prosaic-runtime/issues/37). See [PR #35](https://github.com/B3Cognition/prosaic-runtime/pull/35) for the implementation and review evidence. The release assets carry the source, dependency lineage and qualification receipts.

The unpublished 0.9.24 candidate hit its macOS Intel qualification job timeout during installed-wheel tests. The 0.9.25 qualification allows 40 minutes per native job and runs the same checks.
