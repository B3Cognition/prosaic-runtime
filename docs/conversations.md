# Stateless conversation turns (0.9.0 candidate)

## Optional token-aware context admission

`context_assembly_v1` adds optional `ContextPolicy` and host-supplied
`TokenCounterRegistry` arguments to preparation and execution. Existing calls
keep their version-1 encoding and identity. Opted-in preparation uses
`conversation-turn-v2` and exposes an owned `context_report`, `context_selection`
and `context_work`. Supply the same counter registry when executing a prepared
turn. Durable hosts retain the frozen selection/report and remaining work state.

Choose `fullHistory` to reject overflow, or explicitly choose `omitOldestTurns`
to omit oldest complete turns only from this request. Source history stays
unchanged. Instructions, current user/inputData, tool schemas and current-turn
tool exchanges remain mandatory. Tool continuations that cannot fit terminate
with bounded failure evidence; completed or uncertain effects are preserved and
never automatically retried. Counters operate on copies of final native payloads.

Counters must match the provider, model, pinned tokenizer/template digests and
framing revision, and certify exact or conservative-upper-bound counts. Runtime
ships no universal tokenizer or qualified production counter. Synthetic fixture
counts demonstrate admission behavior only. Provider-reported usage is execution
accounting evidence; Lago receives billing events separately.

Policies also bound candidate checks, cumulative encoded candidate bytes and
active assembly time. Provider waiting is excluded. Time checks are cooperative:
trusted counters must be bounded local code without network I/O or inference.
`context_checkpoint` durably records resource reservations before callbacks;
`on_admitted` lets the host mark dispatch only after successful readmission and
all static checks, before accounting intent and provider effects.

```python
from prosaic_runtime import ContextPolicy

# token_counters is a host-qualified immutable TokenCounterRegistry.
context_policy = ContextPolicy(
    mode='omitOldestTurns', context_window_tokens=32768,
    reserved_output_tokens=2048, safety_margin_tokens=256,
    counter_identity='host-pinned-counter-v1')
prepared = runtime.prepare_turn(agent, message='Next question', history=history,
    policy=policy, context_policy=context_policy, token_counters=token_counters)
result = runtime.turn(prepared, token_counters=token_counters)
```

`conversation_turn_v1` adds `prepare_turn()` and `turn()` to `ProsaicRuntime`.
Runtime owns one bounded model/tool execution. Your host supplies inspected,
approved prose, admitted user input and the complete portable history. Durable
sessions, authorization, idempotency, human approval and recovery belong to
Harness or the embedding service. No graph or new Markdown kind is required.

```python
from prosaic_runtime import ProsaicArtifact, ProsaicRuntime, RunPolicy

# config is host-owned RuntimeConfig. Inspection happens before admission.
agent = ProsaicArtifact.from_inspection(inspected_document)
runtime = ProsaicRuntime(config)
policy = RunPolicy(timeout_s=30, max_provider_requests=2, max_tool_calls=0)

prepared = runtime.prepare_turn(agent, message="Find a player", policy=policy)
first = runtime.turn(prepared)
if first.completed_turn is not None:
    second = runtime.turn(agent, message="The second player",
                          history=first.completed_turn.messages, policy=policy)
```

Preparation snapshots the inspected artifact, approved resources, policy,
history, input data and selected endpoint/tool descriptors. It computes the
first actual provider payload's UTF-8 size without model requests, callbacks or
version probes. Execution readmits the prepared inputs against current bindings;
binding drift fails before effects. `PreparedTurn` has no public deserializer.
Its identity is context identity, not an authorization token or validation proof.
Runtime construction may discover explicitly configured CLI manifests; execution
may perform their documented preparation/preflight probes.

Instructions and approved bundled resources occupy the privileged instruction
role. User messages, failed corrections, selected host input data and previous
tool outcomes stay unprivileged. Turn agents cannot use `{{args}}` substitution.
Only complete successful exchanges enter subsequent history. For an admitted user
message whose answer failed, retain a `ConversationMessage` with `turnStatus:
"failed"` and a closed `failureCode`; discard its partial assistant/tool exchange.
That correction supplies context and never establishes approval or consent.

`TurnLimits` bounds UTF-8 user text (16 KiB), assistant text (64 KiB), completed
turns (128 KiB), full history (4 MiB), actual provider input (192 KiB), JSON depth
(64), nodes (65,536), metadata/events (64 KiB each) and private evidence (4 MiB).
Each value is a positive finite integer and may be reduced by the host. Tool and
provider budgets are finite too, including an explicit zero allowance. Evidence
requires reserved headroom for each possible dispatch. Preparation rejects
oversized input; every subsequent native payload is bounded before its POST.
Full-history turns disable tool-result compaction and fail with `contextLimit`
when history plus the tool exchange exceeds the request limit. Legacy `run()`
keeps its prior compaction behavior. There is no silent window, summary or eviction.

No workspace is required for a tool-free turn or ordinary registered callbacks.
Filesystem/CLI capabilities require an explicit absolute private workspace and
host grants. Missing bindings fail explicitly; no process-directory fallback is
used. Prose, user input and hashes grant no tool authority. Every batch is admitted
in full before its first handler executes. A host interpreting approval must use
a separately sealed tool-free agent and policy with `max_tool_calls=0`, then
validate the proposal and authorize the operation independently.

`TurnResult` contains a sanitized `Result`, a `CompletedTurn` on success, and
private `TurnEvidence`. Failure stdout is empty and there is no partial completed
turn. Safe codes identify admission/execution failure. Provider IDs, headers,
private endpoints, exception text and continuation instructions are excluded
from public metadata. Evidence records known attempts and usage even if accounting,
cancellation or post-effect callbacks fail. Unknown usage/effects remain unknown.
Its optional per-tool `callIndex`, `signature`, `operationNamespace` and
`operationKey` locate the original logical journal record after a lost
acknowledgment; keep this evidence private. A fingerprint alone cannot establish
that an effect completed. Journal reconciliation and business idempotency remain
separate responsibilities.

All streamed text/tool events are provisional. Runtime emits no durable
`turnCommitted` event; only the host's successful receipt/checkpoint adoption can
establish commitment. Critical event-hook failure fails the invocation; ordinary
observer exceptions keep their existing best-effort semantics. Do not automatically
retry an interrupted invocation: a provider or tool may already have performed it.

Install the qualified Runtime 0.9.0 and recorder 0.3.0 candidate wheels in a fresh
environment and run `python -m pip check`, then `python -I
scripts/conversation_wheel_smoke.py` from the source archive. This gate checks
installed ownership, pure preparation and a two-turn synthetic exchange. These
candidate versions are not available through the public index until publication
and downloaded-byte verification have completed. Recorder schema/data remain
unchanged; the adapter now selects the Runtime 0.9.x family.
