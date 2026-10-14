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

Add an optional immutable `ContextPolicy` argument to Runtime preparation and
ConversationBindings. Absent policy preserves the existing preparation and
binding identity paths, serialized contracts and full-history behavior.

An explicit policy declares:

- `mode`: `fullHistory` or `omitOldestTurns`.
- `contextWindowTokens`: positive context capacity for the selected endpoint.
- `reservedOutputTokens`: positive output reservation and actual output ceiling.
- `safetyMarginTokens`: nonnegative additional capacity withheld from input.
- `counterIdentity`: versioned identifier of the admitted counting strategy.

Expose a trusted host-supplied `TokenCounter` interface. It receives an owned
copy of the final provider request and returns a bounded nonnegative input count.
Its descriptor binds provider, model, encoding/framing revision and implementation
version. It declares exact counting or a documented conservative upper bound;
an unsupported or merely heuristic counter cannot certify strict token admission.
The host must retain the counter implementation matching each durable binding.

There is no universal tokenizer fallback. Existing callers need no tokenizer.
Opted-in hosts register a provider/model-specific counter; preparation refuses
unsupported combinations before any provider/tool call. Counters run locally,
are trusted application code and must not perform network I/O or inference.
Counter exceptions produce a closed admission error without exposing content.

The first implementation supports text and the existing neutral tool blocks.
Unsupported image/audio content or unknown provider framing is rejected in the
token-aware path rather than assigned a guessed token count.

Policy serialization and reports use camelCase. New public Python objects may
use snake_case attributes, following current SDK conventions.

## Assembly algorithm

1. Validate all retained history, inputs and existing structural/byte limits.
2. Resolve the endpoint and require a matching admitted counter descriptor.
3. Compute input capacity as context window minus output reserve and margin.
4. Construct and count the complete native request: rendered instructions,
   current message/inputData, retained history, granted schemas and provider
   framing. Enforce output parameters at or below the reserved output ceiling;
   reject incompatible provider-specific output controls.
5. In `fullHistory`, reject if the request exceeds the token or byte ceiling.
6. In `omitOldestTurns`, remove the oldest whole history turn and reconstruct
   and recount the request until both input ceilings fit. Do not estimate token
   savings by subtracting independent message counts.
7. If mandatory content alone cannot fit, reject explicitly. Otherwise seal
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

The algorithm performs at most one initial count plus one count per removable
turn. Existing history/node ceilings bound work. Token counting does not reserve
or consume provider allowance.

## Assembly report and execution

Expose an immutable `ContextAssemblyReport` with version, mode, counter identity,
full-source history digest, selected-history digest, input token count, counting
kind, output reserve, margin, input capacity, final encoded bytes and retained/
omitted turn counts and sequence ranges. Reports contain no prompt text, tool
arguments or retrieved private content.

Runtime readmits the policy and counter descriptor and verifies the final payload
before dispatch. Mutation of prepared projection/configuration is rejected.
Each subsequent provider request following a tool result must also respect the
token and byte ceilings, including tool continuations and output reservation.
The first version fails explicitly if new tool exchanges exceed capacity; it does
not omit exchanges from the current turn or summarize them automatically.

Optional policy changes invalidate a binding. Existing bindings without policy
remain byte-identical. Re-enabling a different counter or widening a window
requires a new explicit binding version, not a silent model-text override.

## Harness persistence and recovery

Harness retains the full admitted source history in the existing owner-scoped
store. Before dispatch, persist the context selection descriptor and report
alongside the allocated attempt. Preserve the full source-history digest as
well as the selected projection identity; a shortened input must not replace the
source identity used for replay/correlation.

Introduce a negotiated conversation storage/encoding version for opted-in
attempts requiring these new fields. Do not add mandatory fields to existing
sealed version-1 session/receipt records. File/PostgreSQL backends must explicitly
support the new version before opting in; schema changes use normal migrations
with separate owner/runtime roles. Existing version-1 data remains readable.

Recovery with a saved receipt adopts it without preparing or dispatching again.
An undispatched attempt may reconstruct only its persisted selection with the
same counter and binding, verifying both identities. A dispatched attempt without
a receipt still requires explicit retry consent. Counter/model changes or missing
implementations fail closed. Retry allocates fresh allowance and persists its own
selection; it never silently repeats an uncertain effect.

Conversation interpretation uses the same explicit policy mechanism and keeps
its existing zero-tool and current-interaction authority constraints.

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
- Tool continuations exceeding capacity stop before another provider dispatch.
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
