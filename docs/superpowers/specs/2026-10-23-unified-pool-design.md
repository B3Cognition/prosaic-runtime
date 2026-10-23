# Unified native/API assignment pool

Approved direction: the user requested implementation in Runtime and validation
in Integration Lab. Claude Code, Codex CLI and local/API endpoints participate
in one ordered pool. Switch only when work provably has not started. Existing
native/API execution, configuration formats and guarantees remain compatible.

## Contract

- An opt-in `UnifiedRuntimeConfig` binds existing native/API configuration objects,
  globally distinct profile aliases, explicit API quota keys and ordered pools.
  File configuration uses `kind: unified` and references existing native/API files
  relative to the unified file. Shared account aliases must share quota keys.
- `UnifiedRuntime.run` requires `PoolPolicy`: one total deadline, a maximum number
  of candidate attempts, a separate native-attempt cap, optional explicit native
  policy and optional API policy. API request/tool caps must be explicit. Each
  backend receives only its own policy, reduced to the remaining deadline and
  remaining API allowances. Native internal requests/tools are opaque.
- Required capabilities filter candidates before execution. Missing native or
  API permission policy makes that backend ineligible. `tools: full` is native
  only; custom API tools are not projected onto native permission modes.
- Native safe fallback uses existing concrete execution evidence. API safe
  fallback requires a transport-proven first-request HTTP 429 rejection or a
  connection refusal before acceptance, with zero earlier tool dispatches.
  Initial tools, any previous response, authentication errors, ambiguous server
  failures, timeouts, cancellation, malformed results and critical callbacks stop
  execution. Best-effort observer events never establish retry authority.
- One public `CapacityStore` supplies persistent, quota-keyed cooldowns. No hidden
  availability probes, subscription exhaustion, server startup or mid-task state
  migration. All unavailable returns promptly with earliest retry time.
- Pool evidence reports every attempt, separate native/API counts, known tokens
  and unknown usage honestly. It does not advertise API/Harness conversation,
  receipt or accounting capabilities for native execution. Backend metadata stays
  backend-owned; pool selection events contain safe structural fields only.

## Validation and ownership

Runtime owns selection, execution policy and safety proof. Lab consumes only
installed immutable candidate wheels and public APIs. Test with real fixture CLI
subprocesses and numeric-loopback HTTP servers, including rejection after work,
budget exhaustion, shared quotas, deadline/cancellation and cross-process state.
Build/install/audit the exact wheel; preserve released manifests and historical
reports. Perform a bounded live Claude/Codex/local selection pilot using unchanged
synthetic Markdown/inputs with controlled private capacity marks. Distinguish
controlled availability from actual provider quota rejection. Only one real
Claude account is present. No publication or release qualification is implied.
