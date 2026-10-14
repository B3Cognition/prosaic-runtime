# Token-aware conversation context assembly

## Purpose and approved scope

Provide reusable Runtime/Harness SDK capabilities, demonstrated and tested by
Integration Lab. The approved first increment adds token-aware conversation
admission with optional omission of oldest complete history turns. Full history
with explicit rejection remains the default. Stored history is never truncated.
System instructions, the current request and granted tool schemas are mandatory.
No model-generated summaries are introduced.

Cost reservations/degradation and trajectory evaluation are separate increments.
This specification does not authorize their implementation or change Core's
Markdown distribution responsibilities.

## Current boundaries

Runtime's `prepare_turn` constructs the native provider payload and checks its
encoded bytes before dispatch. `PreparedTurn` seals admitted material and
execution readmits its configuration. Harness retains bounded complete history,
reserves attempts durably and reconstructs preparation during recovery. Its
existing conversation policy accepts only `fullHistory`.

Byte, JSON, history and receipt limits remain independent safety ceilings.
Context fitting cannot bypass storage capacity or enlarge execution authority.

## Public contracts

Add an optional immutable `ContextPolicy` argument to Runtime preparation.
ConversationBindings exposes separate `context_policy` and
`interpretation_context_policy` arguments and a trusted immutable counter registry.
The former applies only to ordinary turns; the latter applies only to the
interpreter. An absent argument preserves the legacy path for that purpose.
With both absent, preparation/binding identities and serialized contracts remain
unchanged. An interpretation policy without an interpreter is rejected.

The registry maps `counterIdentity` to a descriptor and trusted implementation;
only descriptors, never executable callbacks, enter binding material. Validate
both explicit policies against their resolved endpoints before session creation,
and seal both policies and selected descriptors into the binding identity. The
same counter may serve both purposes only when its endpoint descriptor matches
both. There is no implicit inheritance between the two policies.

An explicit policy declares:

- `mode`: `fullHistory` or `omitOldestTurns`.
- `contextWindowTokens`: positive context capacity for the selected endpoint.
- `reservedOutputTokens`: positive output reservation and actual output ceiling.
- `safetyMarginTokens`: nonnegative additional capacity withheld from input.
- `counterIdentity`: versioned identifier of the admitted counting strategy.
- `maxCandidateChecks`: candidate limit of at least 2, default 128, maximum 256.
- `maxAssemblyBytes`: cumulative encoded candidate-byte allowance, default
  33,554,432 bytes, maximum 268,435,456 bytes.
- `assemblyTimeoutMs`: cumulative active assembly-time allowance, default 2,000
  ms, maximum 30,000 ms; any enclosing execution deadline also applies.

Output reserve plus margin must be strictly smaller than context capacity.

Expose a trusted host-supplied `TokenCounter` interface. It receives an owned
copy of the final provider request and returns a bounded nonnegative input count.
Its descriptor binds provider, model, encoding/framing revision, supported context
capacity and implementation version. Policy capacity cannot exceed that declared
model capacity. It declares exact counting or a documented conservative upper bound;
an unsupported or merely heuristic counter cannot certify strict token admission.
The host must retain the counter implementation matching each durable binding.

There is no universal tokenizer fallback. Existing callers need no tokenizer.
Opted-in hosts register a provider/model-specific counter; preparation refuses
unsupported combinations before any provider/tool call. Counters run locally,
are trusted application code and must not perform network I/O or inference.
Counter exceptions produce a closed admission error without exposing content.
The registry freezes the tokenizer/template asset digests and framing revision
in each descriptor; a mutable model alias alone is insufficient identity.

Runtime owns the fitting algorithm, not a mandatory tokenizer dependency. The
host supplies model-specific counters, directly or through optional integrations.
The first release ships the interface and synthetic fixture demonstration; it
does not advertise a qualified production counter. Before using token-aware
admission with a production endpoint, the host must qualify the registered
counter's framing/count guarantees against that endpoint's pinned model assets.

These counts certify context admission under the host's admitted counting
contract, not actual billable usage. Provider-reported usage remains execution
accounting evidence; Lago receives submitted billing events separately. Lago
does not participate in context fitting or decide which history is omitted.

The first implementation supports text and the existing neutral tool blocks.
Unsupported image/audio content or unknown provider framing is rejected in the
token-aware path rather than assigned a guessed token count.

Policy serialization and reports use camelCase. New public Python objects may
use snake_case attributes, following current SDK conventions.

## Assembly algorithm

1. Validate full-source history against history/storage byte ceilings and JSON
   structural ceilings; validate the current message and inputData against their
   existing individual limits. Do not enforce the full provider-request byte
   ceiling on unselected history here. Optional omission cannot rescue a source
   that exceeds its storage/history ceiling.
2. Resolve the endpoint and require a matching admitted counter descriptor.
3. Compute input capacity as context window minus output reserve and margin.
4. Construct and count the full-history native request: rendered instructions,
   current message/inputData, retained history, granted schemas and provider
   framing. Enforce output parameters at or below the reserved output ceiling;
   reject incompatible provider-specific output controls.
5. In `fullHistory`, reject if the request exceeds the token or byte ceiling.
6. In `omitOldestTurns`, remove the oldest whole history turn and reconstruct
   and recount the request until both input ceilings fit. Do not estimate token
   savings by subtracting independent message counts.
7. If the search reaches a mandatory-only candidate and that candidate cannot fit,
   reject explicitly. Otherwise seal
   the selected projection, policy, counter descriptor and report into preparation.

Existing neutral user messages delimit historical turns. A completed turn's
assistant/tool exchanges stay with its initiating user message; a failed user
turn is an independently removable group. Validation precedes grouping, and no
tool call/result pair or continuation may be separated. Once a suffix fits,
selection stops; it does not opportunistically remove additional turns.

Omission is a projection of history for this request, not deletion, summarization
or a change to past approvals. Current workflow interaction data supplied by the
trusted host remains mandatory inputData. Historical text never grants authority.
Admitted tool schemas cannot be omitted to make a request fit.

Every constructed candidate consumes one candidate
check and its final encoded length from the cumulative assembly-byte allowance.
Before counting, reject a candidate that would exceed either allowance; check
the remaining active-time allowance and enclosing deadline before and after each
construction and callback. Charge only time spent performing assembly; provider
latency and storage/queue waiting do not consume the assembly-time allowance.
Do not assume counter monotonicity or use binary search, and do not reject a
larger candidate based on a mandatory-only count. A full request exceeding the
provider-input byte ceiling may proceed to omission without calling the counter,
but still consumes assembly work allowance.

These limits bound repeated SDK projection/counting work and reject with a closed
failure rather than holding the session lease indefinitely through many suffixes.
They do not sandbox arbitrary trusted callback code or forcibly interrupt a
blocked in-process counter: bounded local counter execution is a host obligation.
Token counting does not reserve or consume provider allowance. The same remaining
assembly work/active-time allowance covers readmission within that preparation/execution
attempt and continuation checks; retries receive a new admitted allowance.

Before sealing, retain allowance for at least one frozen-selection readmission:
one candidate check, its encoded bytes, and a positive active-time allowance.
Reject preparation if that reservation cannot fit. This guarantees available
work for readmission, not that a callback will complete within its deadline.
Harness persists cumulative charges and outstanding reservations as bounded
attempt resource state. Undispatched recovery restores the remaining allowance;
it does not reset charges. Charge checks/bytes before invoking the counter, reserve
an active-time slice before invoking it, and reconcile elapsed active time on
completion. Interrupted work retains its outstanding reservation conservatively;
if no allowance remains, recovery fails closed without provider dispatch.

## Assembly report and execution

Expose an immutable `ContextAssemblyReport` with version, mode, counter identity,
full-source history digest, selected-history digest, input token count, counting
kind, output reserve, margin, input capacity, final encoded bytes and retained/
omitted turn counts and sequence ranges. Reports contain no prompt text, tool
arguments or retrieved private content.

The report also records candidate checks and cumulative encoded candidate bytes.
Version-1 report JSON is bounded to 16,384 bytes and 512 nodes. Counter descriptors
are bounded to 16,384 bytes and 512 nodes; selection descriptors to 8,192 bytes
and 256 nodes. Use compact prefix/suffix sequence ranges, not per-message arrays.
All use the existing finite JSON/depth validation and immutable owned copying.
These are maximum encoded sizes, including their own field framing.

Runtime readmits the policy and counter descriptor and verifies the final payload
before dispatch. Mutation of prepared projection/configuration is rejected.
Each subsequent provider request following a tool result must also respect the
token and byte ceilings, including tool continuations and output reservation.
The first version fails explicitly if new tool exchanges exceed capacity; it does
not omit exchanges from the current turn or summarize them automatically.

### Failure outcomes

Initial preparation failures are admission rejections: `contextLimit` for token,
final-input-byte, candidate-count, cumulative-byte or cooperative-time overflow;
`unsupportedCapability` for unsupported endpoint/framing; `invalidPolicy` for
missing/mismatched counters, invalid counter results or callback exceptions.
The safe `limit` identifies the fixed violated field; exceptions never expose
counter text or prompt content. Harness maps these using its closed error contract
before allocating an attempt or writing an accounting intent.

After dispatch, continuation token/byte/work/active-time overflow produces a durable
failed turn with existing failure code `contextLimit`. Counter/configuration
failure produces existing code `bindingMismatch`. The failure result uses a
nonnegative exit status and has no completed partial assistant exchange. Retain
all provider usage/unknown-usage evidence, tool outcomes and journal references
already recorded. A known completed effect stays completed even when the next
request cannot fit. Existing `toolEffectUncertain` or `toolJournalFailure`
classification takes precedence when the effect outcome is uncertain; context
failure cannot downgrade it to a harmless rejection.
Enclosing execution timeout/cancellation retains its existing classification;
assembly checks do not relabel those outcomes as context overflow.

Neither overflow nor counter failure triggers automatic model retry, omission of
current-turn tool exchanges, workflow resume or effect repetition. A later user
request is a new turn. Any explicit retry after uncertain dispatch/effects follows
the existing acknowledgement, attempt-cap and journal reconciliation rules.

Optional policy changes invalidate a binding. Existing bindings without policy
remain byte-identical. Re-enabling a different counter or widening a window
requires a new explicit binding version, not a silent model-text override.

## Harness persistence and recovery

Harness retains the full admitted source history in the existing owner-scoped
store. Before dispatch, persist the context selection descriptor and report
alongside the allocated attempt. Preserve the full source-history digest as
well as the selected projection identity; a shortened input must not replace the
source identity used for replay/correlation.
The persisted initial report remains immutable. Readmission verifies the frozen
selection's source/projection identities and counter result without regenerating
the original omission search or replacing its report. Readmission work consumes
remaining allowance and enters aggregate final metrics, not prepared-context
identity. Active timing is diagnostic/resource state and is excluded from context
identity comparisons; elapsed provider time cannot change selection identity.

Version-2 admission reserves worst-case additional session bytes/nodes for both
selected descriptors, the selection descriptor and initial/final assembly reports,
plus bounded attempt resource state (at most 4,096 encoded bytes and 128 nodes),
including their containing JSON field framing and digest fields. Receipt admission
reserves the corresponding final report/descriptor material plus failure evidence.
The fixed per-object ceilings above determine this overhead; the codec must
compute the enclosing maximum and reject before dispatch when it cannot fit.
Reserve final failure reports even when no completed turn will exist. Record
aggregate continuation metrics in one bounded final report, not an unbounded
per-request report array. Both reserved and committed bytes/nodes participate in
the existing per-turn, session and total-receipt ceilings.

Opted-in sessions use negotiated session/receipt record version 2 and Runtime
encoding `conversation-turn-v2`, with version-1 assembly reports. Require an
advertised `context_assembly_v1` capability across Runtime, Harness and the selected
store before session creation. Do not add mandatory fields to existing
sealed version-1 session/receipt records. File/PostgreSQL backends must explicitly
support the new version before opting in; schema changes use normal migrations
with separate owner/runtime roles. Existing version-1 data remains readable.

Recovery with a saved receipt adopts it without preparing or dispatching again.
An undispatched attempt may reconstruct only its persisted selection with the
same counter and binding, verifying both identities. A dispatched attempt without
a receipt still requires explicit retry consent. Counter/model changes or missing
implementations fail closed. Retry allocates fresh allowance and persists its own
selection; it never silently repeats an uncertain effect.

Conversation interpretation resolves its own explicit policy/counter and keeps
its existing zero-tool and current-interaction authority constraints. A session
with either purpose opted in uses version 2; an absent policy for the other
purpose still uses its legacy fitting behavior, inside the negotiated version-2
envelope. No version-1 document is silently upgraded in place. New stores read
both versions; old workers must refuse version-2 sessions. Deployment requires
explicit store migration and upgraded writers before enabling the new capability.

## Integration Lab

Demonstrate two host-configured bindings: default full-history rejection and
explicit oldest-turn omission. Requests cannot choose or widen the context policy.
Show a filtered assembly report explaining omitted ranges while the history API
still returns all retained turns. Use installed, pinned SDK packages for adoption.

The first lab gate uses a deterministic synthetic provider and registered fixture
counter to prove the admission/omission contract. Label this synthetic evidence;
it does not certify a production model's tokenizer accuracy. Production counters
need separate provider/model framing fixtures and qualification before use.

## Verification and acceptance criteria

- Default preparation and binding identities remain unchanged for existing calls.
- Token-aware full-history overflow rejects before dispatch; mandatory overflow
  also rejects in omission mode, with zero provider/tool/accounting intent effects.
- Boundary tests cover exact capacity, one-token overflow, UTF-8 text, schema
  overhead, provider framing and output reservation enforcement.
- Oldest whole turns are omitted deterministically; tool exchanges remain valid,
  failed turns keep their status, and stored/full history remains unchanged.
- Counter mismatch, malformed results and callback failures reject safely.
- Distinct ordinary/interpreter endpoints resolve separate counters and capacities;
  absent per-purpose policy preserves legacy fitting and cannot inherit authority.
- Candidate-count, cumulative-byte and cooperative-time limits stop bounded fitting
  before dispatch, including repeated execution readmission and continuations.
- History exceeding final request byte capacity but within source storage ceilings
  can fit through omission; source history/storage overflow still rejects.
- Tool continuations exceeding capacity stop before another provider dispatch.
- Post-effect overflow/counter failure retains actual accounting and journal evidence;
  uncertain effects keep their stronger classification and are never auto-retried.
- Maximum-sized descriptors/reports and failure receipts fit their version-2 reserved
  bytes/nodes; insufficient space rejects before any provider/tool effect.
- A nonmonotonic counter may admit full history even when a mandatory-only
  projection would exceed capacity; no premature precheck rejection occurs.
- Preparation reserves frozen-selection readmission work; undispatched recovery
  restores charges and conservatively retains interrupted reservations.
- Prepared mutation and recovery under a changed model/counter are rejected.
- File/PostgreSQL recovery preserves the selection across replicas and receipt
  adoption makes no new provider call. Version-1 stores/receipts remain compatible.
- Reports exclude content and preserve owner authorization on reads and replay.
- Lab compares task completion on a fixed synthetic full-history versus omission
  dataset, making lost-context failures visible rather than claiming fitting alone
  preserves answer quality. All required lanes fail rather than skip missing SDKs.

## Delivery sequence

Implement Runtime's opt-in contracts and pure assembly first, followed by bounded
continuation checks. Add negotiated Harness/store support and recovery invariants,
then integrate the synthetic lab demonstration and release qualification. Keep
cost/degradation and general trajectory grading out of this release.

This is the written design for user review. Implementation planning and product
code begin only after approval of this specification.
