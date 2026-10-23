# Native execution profiles and pools

**Goal:** Execute canonical Prosaic artifacts through Claude Code or Codex CLI,
with named operator profiles and shared capacity-aware fallback.

**Architecture:** Add an opt-in `NativeRuntime` extension and a separate
`kind: native` configuration. Preserve `ProsaicRuntime`, API configuration,
conversation contracts and all existing guarantees. Native runtimes advertise
only their own capabilities; native permissions are vendor-owned and explicitly
accepted by the host. No claim of API-tool receipts, exact tool budgets, or
portable native sessions. Python subprocesses only; no Node dependency.

## Execution decisions

- Existing API paths and serialized configurations remain unchanged.
- Profiles select executable, backend, model, effort, native settings/config
  directory, maximum workspace permission, and shared non-secret quota key.
- Pools prefer the first available eligible profile, with bounded cooldowns.
  Unknown availability is eligible. Shared quota keys prevent profile aliases
  from bypassing an exhausted account. In-memory state is reusable within a
  host; a locked atomic file store shares cooldowns between CLI invocations.
- One absolute invocation deadline includes all fallback attempts. Cancellation,
  malformed output, authentication errors and uncertain effects never cause
  automatic replay. No tools may have started before automatic fallback.
- Claude uses restricted mode and explicit tools/MCP settings. Native-managed
  policies and settings remain trusted operator inputs. Codex uses explicit
  sandbox/approval settings. Hosts must isolate concurrent writable workspaces.
- Proactive availability can be supplied through the public capacity store;
  no undocumented subscription endpoint or paid probe is used.
- No live inference, publishing, merging, or dependency on Echelon.

## Tasks

1. [x] Configuration and capacity: add `native_config.py`, `capacity.py`, and
   tests for strict config, shared quota cooldown/reset, persistence and locking.
2. [x] Native transports: add `native_process.py`, `native_backends.py`, tests
   using real fixture subprocesses for event normalization, safe fallback
   evidence, output limits, cancellation, timeout and process cleanup.
3. [x] Runtime and CLI: add `native.py`, exports and opt-in CLI routing; test
   artifact admission, profiles/pools, fallback, exhausted pools, effects,
   permissions, critical callbacks and aggregate unknown/known usage.
4. [x] Documentation and qualification: add an untouched standalone example,
   usage guide and changelog entry; run full tests, build and installed-wheel
   smoke. Review the final diff independently of implementation.

## Review focus

Malformed/truncated JSONL must never become success or safe replay; callback
failures must clean up children; aliases must share exhaustion across workers;
reset expiry must not erase newer exhaustion; terminal success must not erase
earlier usage; native mode must never inherit API capability claims.

## Progress

Planning: user approved implementation in Runtime; execute in this session.
Working branch: `codex/native-profile-pools`; preserve existing example edits.
Use the existing checkout on a feature branch; no additional worktree required.

Baseline: 1078 passed, 2 skipped in 365.61 seconds.
Configuration/capacity tests: 11 RED (missing exports), then 11 GREEN.
Transport tests: 11 RED (missing module), then 11 GREEN.
Runtime/CLI tests: 9 RED (missing Runtime/flags), then 9 GREEN; corrected the
CLI fixture to include canonical `execution: agent` metadata.
Safety regressions: 4 RED, then GREEN for real Codex message-only limits, hook
activity, malformed stream blocks and known zero usage before process launch.
Focused suite: 37 passed. No live provider inference has been performed.
Ruling: NativeRuntime is a distinct extension, not a ProsaicRuntime subclass —
preserves concrete API/Harness capability and admission guarantees.
Ruling: Claude requires restricted mode >=2.1.248 rather than silently weakening
execution on the installed 2.1.236 CLI; installation is operator-owned.

Independent review identified three important findings, all reproduced RED and
fixed GREEN: unknown nested Claude event shapes cannot authorize replay;
non-capacity failures are sticky; capacity-lock waits propagate invocation
deadlines and cancellation. Additional regressions reject duplicate JSON fields
and malformed terminal failures. Native `tools: full` compatibility requires
explicit host and operator write grants. Focused tests: 51 passed.

Installed-wheel acceptance passed for native CLI execution/fallback and
cross-process capacity, base ownership/no-Node CLI checks, stateless conversations,
and the existing production fixture. Built wheels and sdist live only under
`/tmp/prosaic-native-qualification`; no release, live inference, or DB call.
The final full-suite run follows the last native admission change; earlier
overlapping verification runs were interrupted once superseded. The earlier
pre-review regression run passed 1115 tests with 2 skips.

Final verification: `.venv/bin/python -m pytest -q` — **1129 passed, 2 skipped**
in 361.42 seconds, after the final `tools: full` change. Build succeeded;
installed native, base, conversation and production acceptance scripts passed.
The final wheel's native modules match the source bytes. `git diff --check`
passed. No independent review findings remain open.
