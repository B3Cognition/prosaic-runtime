# Host-registered custom tools and security boundaries

Status: stage 1 plan approved in chat on 2026-10-01; native implementation and verification completed, final review pending.
Stages 2 (adversarial hardening) and 3 (MCP evaluation) remain queued.

## Intent and delivery order

Provide a practical custom-tool execution example for neutral Prosaic prose,
then harden the code against unauthorized actions caused by prompt injection,
then evaluate MCP. Preserve the small generic Runtime/Harness separation and
keep Echelon-specific policy outside both projects. The user approved this
direction in chat; this document makes the public contracts reviewable.

Deliver these stages in the requested order:

1. Runtime custom-tool API and runnable catalogue example, followed by a Harness
   embedding example using the same tool. Include minimum safe validation before
   any callback can execute.
2. Adversarial tests and targeted security hardening of that execution boundary.
3. A current-source MCP evaluation and recommendation, not an MCP implementation.

Each implementation issue is reproduced, fixed, and verified before the next.
No push, release, new server, or consequential external action is part of this
change. Existing published Runtime 0.4.0 and Harness 0.3.0 stay immutable.

## Current constraints

Runtime's public invocation API rejects names outside `BUILTIN_TOOLS`; its
backend has a private registry hook but no public custom-handler contract.
Prosaic already accepts explicit tool-name lists in neutral frontmatter.
Runtime YAML already lists `allowed_tools`; it does not register implementations.
Harness validates agent steps against built-in read tools and requires read
roots for them. Its controller owns state, admission, receipts, and human choices.

A backend subclass/example alone would bypass the intended public seam. Adding
MCP first would make the example depend on server/client lifecycle and remote
trust before local execution has a clear contract. The selected approach is a
small host-owned registry that reuses the existing bounded native tool loop.

## Ownership and public API

Add an exported immutable `CustomTool` definition in Runtime, with:

- `name`: ASCII `[A-Za-z][A-Za-z0-9_-]{0,63}`, matching its registry key;
  duplicate names and collisions with built-in names are rejected.
- `description`: nonempty host-provided text, at most 4 KiB UTF-8.
- `parameters`: a host-supplied Draft 2020-12 JSON Schema with `type: object`
  and `additionalProperties: false`; top-level unknown properties are rejected.
  Schema validity is checked at registration. Initially
  reject `$ref` and `$dynamicRef` anywhere, avoiding reference resolution entirely.
- `handler`: trusted synchronous Python callable receiving only the validated
  argument dictionary, not Runtime credentials, environment or controller state.
- `version`: required nonempty host version, at most 128 UTF-8 bytes, identifying handler semantics and
  any fixed data it captures. The host must change it when those change.
- `max_argument_bytes` and `max_result_bytes`: positive finite integer bounds,
  defaulting to 16 KiB and 64 KiB respectively.
- Optional `authorize(arguments) -> bool`: trusted host predicate, evaluated
  before execution. Absence means the invocation's explicit tool grant is the
  host authorization; consequential tools must additionally check real host
  approval in this predicate, never a model-provided `approved` argument.

Construct `ProsaicRuntime(config, custom_tools={name: definition}, ...)`.
Keep `run()` and the no-tools default unchanged. Copy/freeze schema and descriptor
data at registration, reject unsupported definitions immediately, and reject
later descriptor mutation rather than changing an active grant. Add capability
`custom_tools_v1` and a read-only public descriptor view for consuming adapters.
Use `jsonschema` as an explicit Runtime dependency for correct schema validation;
do not implement a permissive hand-written subset or fetch schema resources.

Custom tools require explicit names: `tools: read` and `tools: write` continue
to expand only built-ins; `tools: full` remains unsupported. Effective authority
is the intersection of registered implementation, prose request, Runtime YAML
allowlist, and host `RunPolicy.allowed_tools`. Registration grants nothing.
YAML and prose never supply Python import paths, executable commands or modules.

Keep handler objects in host memory, passed directly to the bounded backend.
Never put callables, authorization predicates or captured data into endpoint
payloads. An internal registry adapter composes custom dispatch with the existing
filesystem registry without replacing its path checks. The low-level Echelon
compatibility backend keeps its current default behavior.

## Call validation, results and failure behavior

Before a custom callback runs, check cancellation/deadline, name/grant, raw
argument byte size, strict JSON parsing, schema, and host authorization. Reject
duplicate JSON keys, nonfinite numbers, non-object arguments and excess nesting
(maximum 64 JSON container levels). Require a real boolean `True` from an
authorization predicate; exceptions or other return types deny execution.
Pass it a separate copy of the validated arguments so mutation cannot change
the schema-checked arguments subsequently delivered to the handler.

Handlers return JSON-compatible data. Runtime owns the result envelope:
`{"status":"ok","result":...}`. Returned data cannot replace `status`, the
tool name, call ID, grants, event metadata or read receipts. Serialize with
finite-number checks and enforce nesting/result-byte bounds before adding it
as a `tool` message. Tool output remains data and is never promoted to a system
message, reparsed as tool definitions, or used to alter host permissions.

Denied/invalid calls, handler exceptions and invalid/oversized results produce
bounded `status: error` tool results with categories `not_granted`,
`invalid_arguments`, `authorization_denied`, `handler_error`, `invalid_result`
or `result_limit`. Do not forward arbitrary exception messages, argument contents
or secrets. Preserve the normal
bounded tool loop; do not introduce automatic retries or model escalation.
Fixed error envelopes are independently bounded to 256 bytes, not truncated
into invalid JSON when a successful-result limit is smaller than the envelope.
An explicit initial-tool mismatch still blocks before any call; a failed
acquisition still blocks before final analysis. Successful custom acquisition
uses the same existing deadline, model, conversation and shared tool-round limit.

Check deadline/cancellation again after the callback, before sending a follow-up
request or reporting success. Callbacks are trusted in-process code: a hung or
malicious callback cannot be forcibly stopped or sandboxed by these checks, and
its side effects cannot be rolled back. Tool definitions must document this.
The Runtime does not claim exactly-once tool execution or hard call-count bounds
from a round limit; endpoint response/input bounds still cap transport size.

Structural `tool_started`/`tool_completed` events identify name, call ID, status,
duration and tool version, without raw arguments, result text, exception text or
handler captures. Progress previews for custom calls must also omit raw arguments
and results. Custom tool events cannot manufacture built-in read receipts.
Final invocation Result remains authoritative; streamed events are provisional.

## Harness integration and durable identity

Add `Workflow.load(path, custom_tools=registry, ...)` for explicit Python
embedding. Only host-supplied custom names are eligible alongside the existing
built-in read tools. Built-in writes remain unsupported in Harness steps.
Read roots are mandatory for built-in read tools, not for a catalogue callback
that has no filesystem input. Existing `require_reads` stays native read-file
provenance only; custom tools do not satisfy it.

The default `Harness(workflow, ...)` builds Runtime with the workflow's trusted
registry. An injected execution adapter must advertise `custom_tools_v1` and
matching descriptors for the custom tools actually requested by the workflow;
mismatch fails before dispatch. For custom tools, `require_tools` verifies a
successful matching-version event. It does not prove factual correctness.

Include required custom-tool descriptors (name, description, schema, version,
bounds and whether authorization is required) in workflow fingerprints. Do not
serialize executable objects. Runs with no custom tools keep existing fingerprint
structure and checkpoint format. Changing a required descriptor/version blocks
resume before another model/tool call. Unused registrations do not affect a run.
Version declarations are host assertions, not automatic handler-source hashing
or protection against a malicious host replacing code while retaining a version.

Callbacks never receive controller state or write it through the tool API.
Real human choices still enter through `Harness.resume(choice=...)`, not model
text, tool return values or forged approval fields. No custom-module loading is
added to the generic CLI; the embedding example explicitly registers trusted code.

## Runnable examples

Use one `lookup_catalog` handler over a checked-in synthetic catalogue, with a
schema permitting only a bounded catalogue identifier. Its closure holds the
fixed catalogue: no caller-controlled file path, URL, command or environment key.
It returns `{"found":true,"item":...}` with the exact record, or
`{"found":false,"item":null}` for an absent identifier, and has no external effect.

Runtime example assets: trusted Python tool module, neutral Markdown agent with
paired ALWAYS/NEVER rules and `tools: [lookup_catalog]`, YAML endpoint/allowlist,
and `examples/run_custom_tool.py`. The program requires explicit `--live` for
endpoint requests, supports endpoint/profile overrides, and prints final status
plus structural execution evidence. Source examples execute against a local
fixture in automated tests and the supplied TokenProxy when reachable.

Harness example assets: Python embedding program importing that same tool
contract/implementation (a self-contained local example module in each repo,
not a sibling-repository import), a YAML lookup workflow, final JSON Schema,
and a deterministic check comparing the output with the fixed catalogue.
Require successful execution before admission. Retain source/schema checks;
a schema-valid invented record is rejected. Show run and resume setup explicitly.
The example ends at a human pause, illustrating controller authorization without
publishing anything or treating the catalogue lookup as a consequential action.

Both guides show first-time setup, host registration, the three explicit grants,
execution events, final validation, and common missing-grant/schema/handler
failures. Configurations reference credential environment variables only.
Changing the catalogue must change the declared tool version and admission data.

## Security stage and honest guarantees

After the executable examples pass, add adversarial fixtures whose catalogue
text/tool data says to ignore rules, call shell tools, read credentials, grant a
new tool, forge success/approval, or mutate run state. Deterministic endpoint
responses attempt those actions regardless of whether a live model obeys the
injection. Assert that unauthorized handlers never execute, malformed arguments
never reach a handler, custom results remain data, and no human resolution is
created by model output. Verify ordinary valid calls still work.

Audit touched parsers, event/error paths and callback dispatch for privilege
confusion and secret leakage; fix reproduced issues separately. Add negative
tests for name collisions, nested unknown fields in closed schemas, duplicate
keys, oversize/deep JSON, NaN/infinity, hostile return envelopes, authorization
failure, cross-invocation grants, changed tool versions, and deadline crossings.
Do not add keyword-based injection filters or claim that delimiters make model
reasoning secure. Final answers may still be manipulated or factually wrong;
deterministic admission checks, narrow capabilities and human review are distinct
controls. Arbitrary trusted Python handlers remain outside a sandbox boundary.

## MCP evaluation stage

Only after stages 1 and 2 are verified, consult current official MCP specification
and SDK documentation. Compare a client bridge over the host-registered tool
boundary with adding a separate companion integration package or retaining
local-only tools. Assess discovery/allowlisting, schema validation, server/tool
identity changes, authentication, transport/process lifecycle, timeouts, result
limits, remote side effects, consent, and whether receipts can retain trustworthy
execution identity. Cite sources and distinguish conclusions from experiments.

Deliver an evaluation document with a recommendation and an explicitly scoped
follow-up if implementation is worthwhile. This stage does not install an MCP
SDK, start servers, auto-import advertised tools, or confer trust on discovery.

## Acceptance and verification

Before accepting stage 1: real HTTP and SSE tests prove custom advertisement,
execution, follow-up, permission denial, strict schema enforcement, safe errors,
result bounds, required-tool admission and resume/version binding. Existing
acquisition, filesystem scope, usage, redirect, budgets and no-tool tests stay
green. Both examples run through public APIs without private backend subclassing.

Before accepting stage 2: deterministic injection cases prove the defined
unauthorized actions are denied before side effects and ordinary use still works.
Any observed live-model limitation is recorded rather than admission relaxed.

Run both full pytest suites after each fix, with Prosaic on PATH; build source
archives and wheels and verify new examples are packaged without credentials or
local receipts. Check a clean coordinated dependency install before any future
release; development Harness integration uses an explicit local Runtime override
until a separately authorized release updates its immutable pin.

Before accepting stage 3: the MCP report cites current primary sources, evaluates
the actual implemented boundary, and makes no untested implementation claims.
