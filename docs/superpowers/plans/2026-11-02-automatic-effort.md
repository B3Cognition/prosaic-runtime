# Automatic effort capability discovery

User direction: detect efforts automatically from the selected provider/model,
and keep `ultra` Codex-only. Explicit `supported_efforts` becomes an optional
override rather than a routine profile requirement.

Native higher-effort requests use bounded metadata-only handshakes: Codex
`initialize` / `initialized` / `model/list`, and Claude SDK `initialize` with
`ModelInfo.supportedEffortLevels`. No user prose, thread, model turn, or inference
probe is sent. Successful metadata is cached for 60 seconds (64 entries), scoped
to profile, working directory and an environment digest. Failures are not cached.
Discovery shares the invocation deadline/cancellation, has a 3-second ceiling,
and bounds output to 1 MiB and Codex pagination to four pages.
Host callback failures propagate after cleaning up the owned metadata process,
without falling through to capability defaults or model execution.

Native and unified pools check cheap policy/cooldown eligibility first and probe
only the current candidate, never unused fallbacks. Resolved model capabilities
are pinned into native delegates to avoid repeated discovery.

API admission and conversation preparation remain pure. Standard OpenAI model
responses lack effort metadata, so a dated exact-model catalogue provides
automatic defaults for documented models. Native metadata takes precedence over
that fallback. Dynamic Claude aliases require metadata; they are not guessed
from an offline latest-model assumption. Unknown models retain legacy three-level
support; higher efforts need metadata or an explicit override. Never clamp.

- [x] Restore Codex-only ultra and remove Claude orchestration tool grants.
- [x] Observe failing automatic discovery/default, retry and pool-order tests.
- [x] Implement bounded native metadata, known-model defaults and optional overrides.
- [x] Verify actual installed native CLI handshakes without inference.
- [x] Update documentation, run focused/full and installed-wheel checks, and review.

## Verification

- Native metadata verified against installed Codex 0.163.0 and Claude Code
  2.1.308: gpt-6.1-sol advertised six neutral efforts; sonnet advertised five.
  No user prompt, model thread or inference request was sent.
- Observed and fixed eager/blocked-fallback probes, failed-probe caching, host
  callback exception swallowing, and one-shot cancellation fallthrough with
  controlled subprocess regression tests.
- Final full Runtime suite: 1322 passed, 2 skipped.
- Final isolated installed-wheel discovery/effort tests: 118 passed.
- Wheel and sdist builds and installed native smoke checks passed.
- Independent reviews found no remaining blockers after the control-path fixes.
- Prosaic Core production code and unrelated user edits were preserved.
