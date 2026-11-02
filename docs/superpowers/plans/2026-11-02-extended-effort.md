# Extended neutral effort

Historical implementation phase. The later user direction in
`2026-11-02-automatic-effort.md` supersedes its mandatory declarations and Claude
ultra mapping; current behavior detects support automatically and keeps ultra
Codex-only.

Canonical values: `low`, `medium`, `high`, `xhigh`, `max`, `ultra`.
OpenAI-compatible Chat Completions accept through `max`. For native providers,
`ultra` explicitly selects vendor orchestration: Claude's `ultracode` (dynamic
workflows with xhigh reasoning) or Codex's `ultra` (subagents). This follows the
user's examples of both native modes; they are not equal reasoning budgets.

Add optional `supported_efforts` to model-bound native/API profiles. Omission
retains the existing low/medium/high contract. Explicit declarations are a
subset of the provider's supported neutral values, including an empty set for
models without effort support. Unknown higher-level model support fails closed.
This is operator-supplied model capability metadata, not live discovery or a
hardcoded list of model names. Native/API pools skip incompatible profiles
before dispatch; direct execution rejects incompatible requests. Never clamp.

- [x] Write and observe failing tests for extended HTTP/native projection,
  profile defaults, YAML capability declarations, invalid declarations,
  selected-model admission, model overrides, and incompatible pool members.
- [x] Implement shared pure effort validation, profile capability metadata,
  transport projection, and native/unified pool eligibility.
- [x] Document neutral labels, model declarations, provider differences and
  separate orchestration modes; update changelog.
- [x] Run focused tests, independent review, full Runtime suite, wheel build,
  and installed-wheel checks. Preserve the previous effort work and user edits.

## Verification

- Observed the missing capabilities/projection tests fail before implementation.
- Observed API-default normalization regressions fail, then restored the old
  case/whitespace/disabled-value behavior with strict canonical artifact values.
- Observed Claude ultra permission-boundary tests fail before adding Workflow
  and Agent conditionally; all 18 native projection/boundary cases then passed.
- Final installed-wheel effort tests: 92 passed. Wheel and sdist builds and the
  installed native wheel smoke check passed without live inference.
- Full Runtime suite: 1295 passed, 2 skipped, 1 failed. The failure was
  `test_cli_failures_are_bounded_and_redacted[print("SECRET", file=sys.stderr); sys.exit(2)-cli_exit]`:
  its existing CLI preflight hit `cli_timeout` under the full run. All seven
  cases in that test passed in isolation afterward. The test was not modified.
- Independent review: no remaining blockers after preserving API normalization
  and enabling Claude's workflow tools only for declared/requested ultra.
