# Rule admission in Runtime

Status: decisions approved by the user on 2026-11-05. Implementation authorized on
branch feat/rule-admission; no commit, push or release is authorized.

## Purpose and scope

Prosaic Core PR #4 (main 9d5a02f) added inert enforcement tags and a `rules`
inspection output, with Runtime obligations in Core's `docs/rules.md`: refuse
admission when a declared guard is not registered (mandatory for
`on_violation=fail`), treat reason, rule text and settings as untrusted with
path-like settings inside the project, and consume the rules shape exactly as
merged, including Core's six validation codes. This change implements those
obligations for conversation turns. Everything is off by default.

## Baseline

Runtime main 63b4d49. Core main 9d5a02f (package version still 0.4.1, the same as
the released 0.4.1 that lacks `parse_rules`). The CI test pin moves to 9d5a02f;
`publish.yml` and the dependency floor are left to the maintainer. Untouched-main
suite: 1745 passed, 2 skipped, 2 failed; the two
`test_effort_discovery.py::test_discovery_failure_is_bounded_and_does_not_guess_unknown_models`
cases fail 3 times out of 3 on main and are out of scope.

## Decisions

1. Turns only. `run()`, acquisitions and `NativeRuntime.run` refuse enforce-tagged
   artifacts. Terminal composition is documentation: a final-stage guard could
   only fail on host-owned terminal output.
2. Only the accepted answer enters `CompletedTurn`; `providerAttempts` carries the
   retry's cost. A history block for rejected drafts is a possible Harness follow-up.
3. `before_tool` rewrites are recorded as run (wire, journal, history). `after_tool`:
   fix repairs what the model sees; retry replaces the result with feedback and
   never re-runs the tool; fail keeps effect evidence.
4. One retry per turn. Feedback is an unprivileged fixed envelope with the rule text
   and refused values only.
5. New failure code `ruleViolation`; admission reuses `invalidArtifact` and
   `invalidPolicy`. Harness 0.9.7 and earlier hard-fail on unknown codes after
   provider spend; a companion Harness change is required and out of scope.
6. Rules come from inspection with a `parse_rules` fallback; refuse on `ruleIssues`.
   With an older Core, tagged artifacts are refused and untagged ones are unchanged.
7. Several files: the stronger action's binding wins, with a warning naming both
   sources; identical bindings dedupe; the same action with different settings is
   refused.
8. Generic guard semantics as specified by the maintainer, with the six worked
   examples in the replay fixture.
9. Project root is the Prosaic project directory; guards declare their path
   settings. `allow_unbound_guards` is a constructor argument.
10. Ruff runs on new files only; `CHANGELOG` uses Unreleased.
11. Review follow-up (2026-11-05): `value_in_tool_output` markers default to a vendored
    common ISO 4217 subset plus € £ $, which the `currencies` setting extends; `ISO 9001`
    style text is never read as an amount.
12. Guards declare `fixes`; admission refuses a `fix` binding on a guard that never fixes.
    A declared fixer returning no repair still fails the turn at runtime.
13. Turn identity hashes project-relative path settings; enforcement uses absolute paths.
14. Second review (2026-11-05): bindings resolve per (guard, stage); different stages are
    separate bindings. Decision 7 above applies per stage.
15. `required_guards` values may be `{action, settings?, stage?}`; pins are host-authoritative,
    conflicting prose is refused naming the pin, and pins are validated at startup.
16. `args_reference_seen_ids` reads whole-number floats as integer text on both sides and
    refuses bools and non-whole floats under id keys (security-review finding).
17. Second code review (2026-11-05): refused calls keep call indices; one retry covers a
    whole tool batch; retry/fail are decided only on post-repair verdicts; UnifiedRuntime
    refuses before any backend; `value_in_tool_output` excludes id/date/year/reference keys
    in any case style (implementer extension of the specified list, flagged for review);
    rules.py split into rule_types.py and rule_policy.py to stay under ~500 lines.

## Names chosen by the implementer

`Guard`, `Verdict`, `GuardContext`, `RuleSet`, `Binding`, `compile_rules`,
`rules_fingerprint`, `RuleAdmissionError`, `RuleBindingWarning`, `generic_guards`,
`rule_admission_v1`, `ProsaicArtifact.rules/rule_issues/inspection()`.

See ../../rules-admission.md for the user-facing contract.
