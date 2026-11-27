# Stateless conversation turns

For explicit model/private views, evidence variants sharing the history budget,
and frozen restoration, see [profiled context inputs](context-packs.md).
Default callers retain existing payloads and identities.

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

Install published Runtime 0.9.12 in a fresh environment. Add recorder 0.3.0 only
when durable API-usage accounting is needed. Run `python -m pip check`, then `python -I
scripts/conversation_wheel_smoke.py` from the source archive. This gate checks
installed ownership, pure preparation and a two-turn synthetic exchange. These
versions are available on PyPI; reproducible deployments should verify the exact
release hashes. Recorder schema/data remain
unchanged; the adapter now selects the Runtime 0.9.x family.

## Sent effort evidence

Runtime0.9.18 returns bounded [`reasoning_effort_v1`](sent-reasoning-effort.md) observations. Exact sent controls are separate from requested/default annotations; provider compliance remains unobserved. HTTP call IDs join existing recorder evidence without schema changes. Private durable Result receipts retain this additive metadata.

## Optional malformed tool call correction

By default a tool batch that fails admission (an ungranted tool name, a call of
the wrong shape, arguments that are not a JSON object, too large or outside the
tool's schema) ends the turn with `invalidToolRequest` before any call in it
runs. A Python host may pass `ProsaicRuntime(config, correct_malformed_tool_calls=True)`
to ask the model once per turn to send it again instead (a constructor argument,
like `guards`, so it is not a YAML key):

- Nothing in the refused batch runs, is authorized or enters `CompletedTurn`.
- The next request carries one user-role envelope, after a fixed sentence saying
  it is data, not instructions:
  `{"toolRequestCorrection": {"version": 1, "issue": "argumentsSchema", "callIndex": 0, "tool": "lookup"}}`.
  `issue` is a closed code (`batchShape`, `terminalBatch`, `callShape`,
  `callIdentity`, `functionShape`, `toolNotGranted`, `argumentsMalformed`,
  `argumentsTooLarge`, `argumentsSchema`); `tool` appears only for a granted
  tool. Model text, argument values and validator messages are never echoed.
- The correction honours a host-required first tool (`RunPolicy.initial_tool`):
  when the refused call is that tool, the correction request carries the same
  forcing as the original request, and the fixed sentence asks only for the call
  again with valid arguments, not for an answer without it. A text-only response
  then fails the turn exactly as it would without the correction (`providerFailure`:
  the first-tool choice was not honoured), never as an ungrounded success. For
  other tools the sentence also allows answering without the tool.
- The correction never runs after tool shutdown: once the runtime disables tools
  for lack of progress (repeated identical tool rounds), a malformed batch ends
  the turn with `invalidToolRequest` and spends no further provider request.
- The correction is a provider request inside the existing `RunPolicy` budget:
  with none left, the turn fails as before. A second malformed batch fails the
  turn with `invalidToolRequest`. It is separate from the rule retry allowance.
  Like a rule retry, it runs under the turn's deadline and token budget: if
  either runs out during it, the turn fails with `timeout` or `budgetExceeded`,
  which then stands in for the original `invalidToolRequest`.
- Worst case, one turn spends one correction plus up to three rule retries
  (`rule_retries=3`) beyond its normal requests, always within
  `max_provider_requests`.
- On the wire, the refused response is replaced by a fixed assistant
  placeholder, the bracketed host marker `[host: tool request not accepted;
  nothing ran]` (no model text, no tool calls), before the envelope, so roles stay
  strictly alternating on providers and chat templates that require it.
- `TurnResult.tool_request_corrections` lists the issue code of the correction
  taken; `providerAttempts` counts its request.
- A provider response that cannot be parsed (for example duplicate envelope keys,
  or a native tool block the provider parser rejects) and unencodable arguments
  in a guarded turn stay terminal: they cannot be described without echoing raw
  output.
- Streamed text of the refused response was already delivered to observers, as
  streamed turn events are provisional; only the accepted answer enters history.

The switch is part of the prepared turn's identity only when on, so default
turns keep their bytes; a prepared or frozen turn readmitted under a different
setting is refused with `bindingMismatch`. It does not change
`bounded_tool_recovery`, which applies after a call has been dispatched.

### Constrained Nemotron lookup correction

On an oMLX endpoint serving `NVIDIA-Nemotron-3.5-Lightning-30B-A3B-MLX-4bit`,
enable the following on the trusted OpenAI-compatible endpoint profile:

```yaml
features:
  streaming: false
  omlx_nemotron_tool_correction: true
```

Also enable `correct_malformed_tool_calls=True` in the Runtime constructor or
the host's Harness bindings. The constraint applies when a singleton `lookup`
call fails argument schema validation and the only exposed tool is `lookup`
with a closed, empty object schema. Other tool sets use ordinary correction.
Use the profile flag separately from `omlx_tool_call_envelope`.

Runtime sends oMLX's `structured_outputs.choice` on the correction request and
preserves the host's required first-tool choice. It requires one native
`lookup` call and validates its arguments before the normal authorization and
execution checks. Text, malformed calls and provider errors can end the turn.
The original request and post-tool continuation retain their normal output
controls. The complete recovery needs capacity for three provider requests:
the original attempt, the correction and the continuation. It uses the same
time, token and tool budgets. Recovery receipts retain the original rejection
code, and tool delivery and answer guards supply evidence of the actual read
and grounding.

The setting is opt-in and part of the prepared endpoint profile identity.
Streaming, other model identifiers and other provider protocols are outside the
qualified configuration.

### Constrained tool request correction

For other tool shapes, a trusted nonstreaming OpenAI-compatible profile can
constrain the correction request to a resend Runtime can accept:

```yaml
features:
  streaming: false
  constrained_tool_correction: json_schema   # or omlx_grammar
```

Also enable `correct_malformed_tool_calls=True`. The value selects the wire:
`json_schema` sends `response_format` with a strict `json_schema`, for servers
with constrained decoding such as vLLM; `omlx_grammar` sends oMLX's
`structured_outputs.json`. The setting is refused with streaming, on the
Anthropic protocol and together with `omlx_tool_call_envelope` or
`omlx_nemotron_tool_correction`.

The constraint applies to a refused singleton call. The resend has the form
`{"tool": ..., "arguments": ...}`:

- `argumentsSchema` on a granted tool: `tool` is that name and `arguments`
  follows that tool's schema.
- `toolNotGranted`: one `anyOf` branch per granted tool links each name to its
  arguments. When the branches exceed the bounds or a granted tool's schema
  cannot be carried, only the name is constrained, to an `enum` of the granted
  tools. A required first tool narrows this to that tool.

The wire schema keeps only `type`, `properties`, `required`,
`additionalProperties`, `items`, `enum`, `const` and `anyOf`. Other keywords,
such as `maximum`, `maxLength`, `pattern` or `format`, are left out, so the
constraint can be looser than the tool's schema; batch admission still checks
the real schema. OpenAI's hosted strict mode requires every property to be in
`required`. Tools with optional properties may be refused there; vLLM-style
servers accept them. The hosted strict mode also requires an object at the
root, so the per-tool `anyOf` form may be refused there too. Runtime does not
rewrite schemas.

The derived schema obeys the 16 KiB schema limit, the JSON depth limit of 64
and the 65,536 node limit, measured on its canonical compact encoding. A schema
outside them, a schema shape the constraint cannot carry (such as the tuple
form of `items`), another issue code or a multi-call batch keeps ordinary
correction. The correction request sets `tool_choice` to
`"none"`, so the reply is the constrained content. Runtime parses it, checks it
against the derived schema and turns it into one host-made call with an
identifier unique within the turn. That call then passes the existing argument,
authorization and budget checks. Anything else, including text or a native
call, ends the turn as an unhonoured tool choice. Runtime does not assume the
server applied the constraint. The original request and the continuation keep
their normal output controls. The recovery uses the same single correction and
request budget: the original attempt, the correction and the continuation.

When the setting is on, the result metadata carries
`tool_request_correction_constraint`. It records the wire, the issue code, the
scope (`arguments` or `name`), the SHA-256 of the schema bytes as sent in the
request body and the sorted names of the dropped keywords. At most 32 names in
1,024 bytes are listed; `droppedKeywordsOmitted` counts any others. On fallback
it records the reason (`oversize`, `unsupported`, `issue` or `batch`) instead.
The record is present on every exit path of a turn whose correction ran,
including cancellation, deadlines and limits. The setting is part of the prepared
endpoint profile identity. Without it, requests, prepared turns, identities
and receipts are unchanged.
