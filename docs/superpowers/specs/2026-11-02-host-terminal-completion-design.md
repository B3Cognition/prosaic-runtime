# Host-owned terminal completion in native tool loops

Status: approved by the user on 2026-11-02. Implementation and release preparation
are authorized. The user will integrate and perform product testing.

## Purpose and scope

An authorized host tool can finish a model-driven task with a validated product
artifact. Requiring another model response solely to acknowledge completion adds
cost and permits malformed prose to turn completed work into a failed workflow.
Provide an explicit host-owned completion path in Prosaic Runtime and Harness.

The consumer incident establishes correct interpretation, successful tool statuses
and malformed terminal text. It does not establish an already persisted,
host-validated result. This work prevents that class of future failure; it does
not convert the rejected incident receipt into success.

The user authorized preparing a fix and new releases. The user will integrate
the released packages and perform product testing. Product code, live football
API calls, application repair routing, context retention and search explanations
are outside this delivery. Core Prosaic requires no implementation change.

## Baseline and compatibility

The original working copies are behind remote release tags and contain unrelated
changes. Use isolated checkouts from freshly fetched upstream revisions. Preserve
all existing working files, tags and historical release evidence.

At investigation time the latest GitHub releases report Runtime 0.9.9 and
Harness 0.9.5. Runtime main also has tagged 0.9.10 reasoning-effort work. Recheck
publication state before choosing version numbers; never overwrite a version or
publish unrelated unqualified work. Expected next patch versions are Runtime
0.9.11 and Harness 0.9.6 if intervening versions remain reserved.

Fetched source baselines: Runtime d6b23cd4fee5206e569647b5a057a42ff266a8f7;
Harness c9787a6d8c56ba6a39db2b50a0de27c441d96a65. Runtime 0.9.10 candidate
qualification run 41491238553 succeeded. Its promotion run 41493574309 failed
at the public index verification step with `published distribution is missing`.
Do not claim that tag is a published release or alter its version/bytes. Harness
0.9.5 publication run 40860799677 succeeded. These are observations, not
qualification of the proposed new releases.

The latest APIs include model-free Runtime.run_tool and Harness tool steps. Keep
those for fixed host-admitted operations. StructuredToolLoop already permits
host callback completion but is a separate JSON transport. This change adds the
equivalent authority boundary to native provider function calling through
ProsaicRuntime.run. It does not silently change transports.

Keep the existing checkpoint version 2 and receipt version 1. Add optional,
versioned Runtime metadata and tool descriptors; omit disabled defaults from
fingerprint material so existing workflows retain their identity. Use serialized
camelCase for new public evidence documents. Existing Python API style remains
snake_case. The unchanged Core and PostgreSQL adapter packages are compatibility
witnesses, not automatically new releases.

## Authority and public contract

The following names and narrow initial scope are implementation choices proposed
by the author, not names selected by the user.

1. Export a frozen, owned `TerminalCompletion(output, *, artifact)` type from
   Runtime. Both fields are required bounded JSON snapshots independent of later
   mutation. Output is the workflow response and may remain an acknowledgement
   under an existing response schema. Artifact is the actual host-validated
   product result, separately bound into the receipt. An acknowledgement alone
   is insufficient. Apply fixed construction depth/node/byte bounds and the
   configured tool limit to the combined output, artifact and binding.
2. Add keyword-only `terminal=False` to CustomTool. The true flag is part of the
   host-owned tool descriptor, sealed into workflow identity. Registration by
   itself does not grant completion authority.
3. Add `allowed_terminal_tools=frozenset()` to RunPolicy and WorkflowPolicy.
   These are host controls, subsets of ordinary allowed tools. Neither model
   text, client workflow data, Markdown nor backend result dictionaries can
   set or elevate them. Effective authority also intersects actual step grants
   and Runtime configuration. Reject unregistered terminal grants and grants to
   tools without terminal capability. If a terminal-capable tool is effectively
   available to the model without the completion grant, reject admission before
   dispatch. Unused registrations do not activate the feature. Native metadata
   describing a terminal artifact is not itself a grant.
4. Only the exact public TerminalCompletion type, returned by a registered,
   authorized, terminal-enabled host handler under the explicit host grant,
   requests completion. Fields such as complete, finalArtifact or terminal in
   normal dictionaries and tool text retain ordinary data semantics.
5. A terminal-capable handler may return ordinary JSON when work is incomplete;
   the native loop then continues normally. Invalid or oversized typed completion
   fails closed and is not transformed into a normal model correction.
6. The host validates domain completeness before constructing TerminalCompletion.
   The initial release requires the actual inline artifact. External artifact
   references are deferred: a digest alone does not prove availability, ownership
   or immutability. References require a separately versioned resolver/validator
   contract. Runtime cannot infer domain success from artifact field names.

For an agent step with effective terminal grants, successful completion requires
the typed terminal candidate and its full artifact binding. A normal model final
response, including schema-valid complete=true, must fail as missing terminal
completion rather than fall back to legacy success. Preserve its receipt and
block without automatic tool replay. Steps with no effective terminal grant keep
their existing model-final response behavior. This makes activation opt-in while
preventing model acknowledgement text from bypassing the new evidence boundary.

Runtime advertises a new terminal_tool_v1 capability. Harness requires it at
admission when terminal completion is enabled. Declared tool versions must change
when completion semantics change. RuntimeAdapter must preserve the capability,
restricted authority and genuine completion evidence. Latch the first terminal
candidate in its private attempt ledger; reject further execute calls before
provider/tool dispatch even when ordinary attempt budget remains. Successful
adapter output must match the delegate's output and artifact binding. Callback
failure, output rewriting or attempted redispatch cannot become terminal success;
preserve incurred usage and known completion evidence. Permission reductions
intersect terminal grants with remaining ordinary grants.

## Native execution and accounting

The handler runs through existing argument validation, authorizers, contextual
scope, tool budgets and deadline/cancellation boundaries. Completion is a typed
host result carried outside the provider-visible JSON parsing path.

After a valid terminal handler result, Runtime records the actual successful tool
completion and returns its owned output without another
provider call. Output is one JSON value. The model's prior prose is never used as
the completed result. Keep all already incurred provider requests, tool dispatch
counts, token usage, accounting lineage and uncertainty exactly as recorded.

Retain bounded versioned terminal evidence in Result metadata and the matching
critical tool event: invocation identity, tool name/version, tool call ID and
turn, argument digest, output digest, artifact digest and completed outcome.
Do not call the tool call ID a provider-request ID or invent an unavailable
provider identifier. Private Result metadata additionally carries the owned
artifact; the critical event carries only its binding/digest. Best-effort
observer telemetry receives no artifact or new product-data fields.

Define one closed versioned shape for new completion evidence. Hash UTF-8
canonical JSON using sorted keys, compact separators, ensure_ascii=False and
allow_nan=False. Reject invalid Unicode, nonfinite numbers and over-bound JSON.
Apply caps to actual encoded bytes. Existing hashes retain their existing
recipes. Argument digests are audit bindings, not business idempotency keys.

The private receipt stores both output and the full bounded artifact. Harness
requires a unique matching start/completion pair, matching name/version,
turn/call ID, invocation scope, argument digest and both content digests. Stray
events, duplicate identities and conflicting event/metadata fail closed.

Cancellation, expiration, callback failure, unknown effects, storage failure and
schema rejection remain failures or uncertainty. Record known completion evidence
when available, but never let a terminal request override cancellation or budget
failure. Critical event-hook errors must not become a successful result.

Capture typed output/artifact and provenance in private invocation state before
the post-handler cancellation/deadline check. Record known actual tool completion
separately from invocation failure. Cancellation after capture remains a failed
invocation, retaining the bounded artifact/provenance when they fit; it never
places successful workflow output in stdout or advances the step. Without a
captured result, effect completion remains uncertain.

If limits prevent storing the full artifact, retain a fixed-size completion
breadcrumb and actual provider/tool counts, known token usage/completeness and
accounting lineage within reserved failure headroom. State explicitly that the
artifact was not persisted. Such a failed receipt can never admit success.
Never silently strip provenance or substitute zero/known usage merely to fit.
Complete-artifact recovery is not promised when limits or storage failure
prevented artifact persistence.

## Multiple calls, journals and other entry points

The first release supports a safe singleton boundary. If a provider batch has
more than one call and any call targets an effectively terminal-enabled tool,
reject the entire batch before any sibling dispatch. Do not execute a prefix,
discard siblings, or claim unexecuted siblings ran. Ordinary multi-tool batches
retain their existing behavior. Enable whole-batch validation on every feature-
enabled native round before effects, including ordinary ProsaicRuntime.run.
Current code unconditionally validates the batch in role-aware execution, which
this feature does not use. Apply the new boundary to OpenAI-compatible and
native Anthropic providers, streaming and nonstreaming.

The initial release rejects an effective terminal tool with operation_key before
any callback, journal claim or provider request. Existing journal outcomes have
closed JSON schemas without typed completion provenance; extending that contract
is separate work. This is a fixed release boundary, not an implementation-time
choice. Nonterminal journaled prerequisites remain supported under existing
semantics and are not replayed when a saved Harness receipt is adopted. There is
no cross-invocation business deduplication promise for a nonjournaled terminal tool.

Do not expand the new capability to role-aware conversation.turn, StructuredToolLoop,
native CLI agents or UnifiedRuntime in this release. Unsupported enabled
combinations reject an effective terminal-capable registration before provider/
tool execution even when the caller omitted the new completion grant. Do not
discover a typed result only after effects. Existing disabled paths and unused
registrations remain compatible. Model-free run_tool also rejects an effective
terminal-capable registration before effects and keeps its ordinary contract.

## Durable Harness acceptance and restart

Use the existing pending invocation and immutable RunStore receipt sequence:

1. Persist the pending invocation under the current run lease and fingerprint.
2. Run the native loop. Runtime returns a host-owned terminal candidate, with
   actual tool events and fully finalized accounting metadata.
3. Check lease ownership and persist the sealed bounded receipt containing the
   terminal output, actual inline artifact and their execution binding.
4. Validate receipt identity/provenance, effective terminal permission, actual
   matching tool completion and output digest. Apply ordinary output schema,
   required-tool/read checks and semantic validators.
5. Only then bind output, record acceptance and advance the workflow.

Run the same terminal provenance checks during ordinary status/ledger validation
and receipt recovery, not only first acceptance. A completed invocation entry can
also denote a rejected candidate; it is not proof that its artifact was accepted.
For host retrieval, reuse public Harness.recovery_evidence and select the receipt
whose ID matches the accepted step's output binding. Validate that relationship
before exposing the artifact. Do not add private-store imports, repeat the search
or surface an artifact merely because its failed receipt contains one. The host
remains responsible for run authorization and safe product response projection.

Runtime exit zero means an execution candidate, not persisted workflow success.
Harness does not need a new storage engine or a speculative new completion store.
Never mark success before receipt storage. Product validators must check the
actual artifact even when stdout is an acknowledgement. Add optional
CheckContext.terminal_completion containing the current admitted candidate's
artifact/provenance, defaulting to None for ordinary validation. Provide an
independently owned context for each terminal validator call, so one callback
cannot change the next callback's evidence or mutate the receipt. Reuse the
registered/versioned step validators; consumers configure checks of result
completeness, ownership and actual executed criteria.

A crash after receipt save and before advancement is recovered by loading and
validating that same receipt. No provider call or tool execution is repeated.
Preserve original receipt identity and accounting on adoption. A crash before
receipt storage remains the existing explicit interrupted/uncertain boundary;
there is no claim of atomicity between an external effect and a Harness receipt.
Do not automatically replay an invocation with uncertain effects.

After terminal completion, schema/semantic rejection or missing required evidence
must block rather than automatically re-entering the agent and repeating tools,
even if the agent step's ordinary max_attempts would allow another attempt.
Preserve the rejected receipt for inspection. Explicit host resolution stays
separate from successful completion. Receipt adoption can run validation again
after a crash. Validators must be cooperative, deterministic read-only checks;
they must never invoke the completed search or perform effects. Do not promise
exactly-once validator execution.

## Verification and release

Start with a deterministic provider fixture: call ordinary prerequisite tools,
then one admitted terminal tool. Configure a malformed model acknowledgement
after it and assert that acknowledgement is never requested. Assert the complete
stored artifact/binding and exact accounting, not only exit status or tool names.

Cover valid and invalid completion; JSON-field spoofing; missing host grants;
authorization denial; schema/semantic failure; receipt/output/event limits;
cancellation before/after handler completion; mutation after construction;
acknowledgement-only attempts without an artifact; private-artifact exclusion
from telemetry; provider accounting including unknown usage and bounded failure
fallback; adapter redispatch/output-rewrite attempts and narrowed grants;
unsupported entry points; terminal tools in multiple-call batches; duplicate IDs;
binding/version changes; crash before receipt save and after save; fresh-instance
recovery; and pre-dispatch terminal+journal admission denial.
Also test a schema-valid model acknowledgement without terminal provenance and
accepted-artifact retrieval after restart, including rejection of artifacts from
failed/unaccepted receipts.
Assert rejection never automatically repeats successfully completed effects.

Run full Runtime/Harness source suites, builds, fresh installed-wheel smoke and
dependency checks. Extend the installed-wheel smoke with a native terminal
workflow and fresh Harness recovery. Use existing release workflows for the
required platform, dependency-floor and PostgreSQL compatibility qualification;
publish only the exact qualified artifacts. Preserve existing adapter packages
unless a genuine compatibility change requires a separate release.

Deliver the two release URLs and coherent install pins, artifact checksums,
verification results and a small public-API consumer example. The integration
example shows registering a terminal tool, enabling host policy, returning an
acknowledgement plus a separate inline artifact, and validating it through
CheckContext.terminal_completion. Explain the singleton boundary, journal/
reference restrictions and before-receipt uncertainty window. The user then tests
the exact two-turn Qwen journey, season/league changes, empty/error outcomes,
reload and cross-replica continuation in their product.
