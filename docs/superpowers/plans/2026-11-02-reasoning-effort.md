# Reasoning effort implementation plan

**Goal:** Carry canonical Markdown `effort` into supported execution settings,
with explicit artifact values taking precedence over profile defaults.

**Architecture:** Prosaic retains its permissive string frontmatter contract.
Prosaic Runtime owns provider validation and invocation settings. The portable
execution levels remain `low`, `medium`, and `high`; omitted effort uses the
profile setting, then the provider default. No provider execution enters Core.

## Tasks

- [x] Add real subprocess tests for Claude `--effort` and Codex
  `-c model_reasoning_effort=...`, covering explicit effort, profile defaults,
  omission, invalid values, repeated invocations, and native pool fallback.
- [x] Observe precedence/invalid-type failures, then fix native effort admission
  and resolution without mutating configured profiles.
- [x] Verify OpenAI-compatible HTTP payloads for both stateless execution and
  conversation execution, including profile defaults and artifact overrides.
- [x] Document the three mappings and the distinction between Runtime invocation
  and target file distribution. Native providers consume Prosaic inspection and
  pass rendered prompts on stdin; no target-folder copies are needed.
- [x] Run focused tests, full affected suites, builds, and installed-wheel smoke.

## Review focus

An omitted value must not produce an effort flag; malformed list/dict values must
raise ValueError before dispatch; a profile default must not hide an explicit
artifact setting; pool fallback must preserve the same artifact request; one
invocation must not change defaults for subsequent invocations. Existing historic
reference evidence and unrelated working-tree changes remain intact.

## Verification

- Observed the native override and malformed-list/dict tests fail before the fix.
- Installed-wheel effort tests: 36 passed, using real local HTTP and subprocess
  boundaries without model inference.
- Core full suite: 581 passed. Runtime final full suite: 1240 passed, 2 skipped.
- Core and Runtime wheel/source builds succeeded. Core wheel smoke and Runtime
  native wheel smoke passed in a fresh environment.
- Independent review: no blocking findings.
- The first Runtime full run had one timing failure in
  `test_running_cli_is_stopped_with_child_process_cleanup[deadline]` while Core
  tests ran concurrently. It passed both an isolated rerun and the final full run.
