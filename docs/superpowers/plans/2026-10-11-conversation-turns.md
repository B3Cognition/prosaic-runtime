# Conversation Turns Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute one bounded role-aware conversational turn through the existing Runtime engine, with portable history and preserved failure evidence.

**Architecture:** Pure preparation snapshots instructions, unprivileged history and effective policy before execution. Invocation-local options adapt the existing shared loop; both transports use that loop, including tool-free turns. An independent bounded accumulator retains observed usage and tool outcomes through every failure exit.

**Tech Stack:** Python 3.11+, existing Prosaic, jsonschema, pytest, build and credential-free loopback HTTP fixtures. No new production dependency.

**Spec:** [Approved conversation design](../../../../prosaic-harness/docs/superpowers/specs/2026-10-11-conversation-design.md).

## Global Constraints

- Existing `run()` rendering, prompt roles, acquisition and serialized results remain compatible.
- Serialized new public contracts use camelCase; Python APIs use normal snake_case.
- No new repository, distribution, Markdown kind, chat CLI, execution engine or provider-managed session.
- Initial transports: OpenAI-compatible and native Anthropic text/tools; require `conversation_turn_v1`.
- No workspace by default. File/CLI capabilities require explicit private host workspace/root bindings; no process-current-directory fallback.
- Canonical prose containing `{{args}}` in its body or bundled resources is rejected for turns.
- Historical tool requests are context, never commands to redispatch. Instructions and user/tool data keep separate roles.
- Entire malformed/ungranted tool batches fail before effects with `invalidToolRequest`; legacy correction behavior remains compatible.
- Full-history turns disable compaction per invocation regardless of endpoint features; bound actual provider encoding before every request.
- Defaults: 16 KiB user text, 64 KiB final assistant text, 128 KiB complete turn, 192 KiB model request; all finite and host-owned.
- Preserve recorder contracts and Core 0.4.1 formats. Native Python production code only. Use apply_patch; observe failing tests before implementation.

## Review Focus

- JSON/SSE normalization must not synthesize or discard malformed tool fields before strict batch admission (Task 3).
- Consecutive failed user corrections must survive native-provider encoding without a fabricated assistant answer (Task 2).
- A journal commit followed by cancellation must retain the known completed effect (Task 5).
- Accounting persistence failure must not erase an already observed provider usage snapshot (Task 5).
- Preparation and dispatch must measure the same native envelope, including escaping, schemas and current exchanges (Tasks 1 and 4).

## Frozen interfaces and records

Create `src/prosaic_runtime/conversation.py` with immutable, owned encoded records. Each `to_dict()` returns a new nested object; finite JSON validation precedes copying/hashing.

| Public name | Contract |
| --- | --- |
| `ConversationMessage.from_dict(document)` | Closed version-1 message; `role` is user/assistant/tool, with ordered `content` blocks |
| `CompletedTurn.from_dict(document)` | Closed `{version: 1, messages: [...]}` complete new turn; no partial groups |
| `TurnEvidence.from_dict(document)` | Bounded private execution evidence for the embedding host; counters, known/unknown usage and known/uncertain tool outcomes |
| `TurnResult` | `result: Result`, `completed_turn: CompletedTurn | None`, `evidence: TurnEvidence`; failed results have no completed assistant group |
| `TurnLimits` | `max_user_bytes=16384`, `max_assistant_bytes=65536`, `max_turn_bytes=131072`, `max_input_bytes=196608`, `max_history_bytes=4194304`, `max_json_depth=64`, `max_json_nodes=65536`, `max_metadata_bytes=65536`, `max_event_bytes=65536`, `max_evidence_bytes=4194304` |
| `PreparedTurn` | Owned artifact/history/input-data/policy projection, public `encoding_version`, `context_identity` and actual first-request `input_bytes`; positively readmitted before execution |
| `TurnAdmissionError` | Safe typed ValueError with `.code` and optional `.limit`; closed codes `invalidHistory`, `invalidArtifact`, `invalidPolicy`, `bindingMismatch`, `unsupportedCapability`, `inputLimit`, `contextLimit` |

Message content blocks are closed unions:

```python
user = {"role": "user", "content": [{"type": "text", "text": "I meant Prague"}],
        "turnStatus": "failed", "failureCode": "providerFailure"}
assistant = {"role": "assistant", "content": [
    {"type": "text", "text": "Searching"},
    {"type": "toolCall", "callId": "opaque-neutral-id", "name": "lookup", "arguments": {}}]}
tool = {"role": "tool", "content": [
    {"type": "toolOutcome", "callId": "opaque-neutral-id", "outcome": {"status": "ok", "result": {}}}]}
```

Preserve block order, especially interleaved native text/tool requests. Historical user status is `completed` or `failed`; cancellation is a bounded failure code. Only user history records carry these fields. Encode failure status as unprivileged user data. Partial native continuations and physical provider IDs never enter public history. Derive neutral call identity from invocation/exchange/index; adapters allocate matching private native IDs per request and allow native `call_0` to repeat across exchanges.

Add the following public Runtime methods and exports:

```python
def prepare_turn(self, artifact: ProsaicArtifact, *, message: str,
                 history: tuple[ConversationMessage, ...] = (), input_data: dict | None = None,
                 policy: RunPolicy | None = None, limits: TurnLimits | None = None,
                 workspace: str | Path | None = None) -> PreparedTurn: ...

def turn(self, artifact: ProsaicArtifact | PreparedTurn, *, message: str | None = None,
         history: tuple[ConversationMessage, ...] = (), input_data: dict | None = None,
         policy: RunPolicy | None = None, limits: TurnLimits | None = None,
         workspace: str | Path | None = None, on_event=None, cancelled=None, env=None,
         context=None, accounting=None, observer=None,
         operation_context: InvocationScope | None = None, tool_journal=None) -> TurnResult: ...
```

`input_data` is bounded host-provided unprivileged data, used for an exact workflow interaction view; it is distinct from accounting `context` and raw user text. It counts toward the request/record work bounds. Prepared form forbids overriding message/history/input_data/policy/limits/workspace. Preparation performs no environment-secret reads, HTTP, inspection subprocesses, CLI probes, callbacks, journal methods or schema migration. It uses bundled resource contents already present in `ProsaicArtifact`. Revalidate prepared material, current effective configuration and descriptors before dispatch; a caller-supplied matching hash alone is insufficient.

Preparation rejects oversized provider input with `TurnAdmissionError(code='contextLimit', limit='maxInputBytes')`; other input bounds use `inputLimit` and their explicit field name. Messages expose no raw parser/provider error. Durable consumers persist original owned public inputs, the sealed binding/policy/TurnLimits identity and original context_identity/encoding_version. A new worker reconstructs messages with ConversationMessage.from_dict(), calls public prepare_turn() again with the original inputs and exactly reconstructed binding, and requires both identities to match before turn(prepared). Do not deserialize an arbitrary PreparedTurn or import Runtime internals. Physical workspace/root paths remain private execution mappings; if configured in Harness, a stable logical workspace identity seals their resource meaning.

Default turn `RunPolicy`: timeout 120 seconds, max_tool_rounds 8, max_provider_requests 8, max_tool_calls 16, max_input_bytes 196608. Explicit policies must have finite provider/tool limits; zero is valid. Apply the stricter policy/TurnLimits/transport bound. Legacy nullable caps remain accepted by `run()`.

Public Result metadata allowlist for turns: `failure_reason`, `provider`, `finish_reason`, `streamed`, `token_usage_details`, `invocation_budgets_v1`, and sanitized accounting attribution/coverage. Public stderr is a fixed failure code/message, never raw headers, error bodies, prompts, endpoints, continuations or provider call IDs. `TurnEvidence` is host recovery data, not a chat response; its closed schema includes `version`, `providerAttempts`, `toolAttempts`, `knownUsage`, `usageComplete`, `toolOutcomes`, `failureCode` and original accounting correlation. It cannot invent recorder completion or business reconciliation.

### Task 1: Immutable records and pure preparation

**Files:** Create `src/prosaic_runtime/conversation.py`, `src/prosaic_runtime/conversation_preparation.py`, `tests/test_conversation_contracts.py`; modify `admission.py`, `runtime.py`, `__init__.py`.

**Interfaces:** Produce all records above and `prepare_turn()`. Preparation produces the exact adapter-ready initial projection and identity consumed by Tasks 2–5.

- [ ] Write contract failures before exports exist:

```python
def test_user_status_is_owned_unprivileged_data():
    from prosaic_runtime import ConversationMessage
    raw = {"role": "user", "content": [{"type": "text", "text": "I meant Prague"}],
           "turnStatus": "failed", "failureCode": "providerFailure"}
    record = ConversationMessage.from_dict(raw)
    raw["content"][0]["text"] = "changed"
    assert record.to_dict()["content"][0]["text"] == "I meant Prague"
    assert record.to_dict()["role"] == "user"
```

- [ ] Run `.venv/bin/python -m pytest tests/test_conversation_contracts.py -q`; require missing API/contract failures, not infrastructure failures.
- [ ] Implement closed unions, finite encoded snapshots, complete exchange pairing and pure preparation. Reject unknown fields/roles, orphan or incomplete results, duplicate exchange IDs, cycles/aliased amplification, nonfinite JSON, invalid Unicode, malformed policies and `{{args}}` in body/resources. Reuse existing routing and static tool schema validation. Preparation reads no host callbacks or filesystem resources.

```python
# Preparation order is mandatory; no model/tool code is reachable here.
owned_history = validate_conversation_history(history, limits=limits)
owned_agent = validate_conversation_artifact(artifact, config=self.config)
projection = build_turn_projection(owned_agent, owned_history, message, input_data, policy, limits)
payload = encode_initial_turn_payload(projection)
prepared = admit_prepared_turn(projection, payload, self.config, self.tool_descriptors)
```

These private helpers live in `conversation_preparation.py`; `validate_conversation_history` lives in `conversation.py`. Each returns owned standard data and accepts no executable callbacks.
- [ ] Verify exact Unicode/escaping overhead and template/resource rejection. Run contracts plus `tests/test_artifact_admission.py`; review and commit this task.

### Task 2: One shared role-aware provider loop

**Files:** Modify `execution.py`, `openai_compatible.py`, `anthropic.py`, `provider_types.py`, `runtime.py`; create `tests/support/conversation_server.py`, `tests/test_conversation_turns.py`.

**Interfaces:** Produce `turn()` and internal `_run_prompt_with_tools(..., initial_messages=None, execution_options=None, turn_capture=None)`. Default options preserve legacy paths. `execution_options` is invocation-local, never provider metadata supplied by a model.

Define test helper `ConversationFixture(provider)` using a real bounded loopback server, adapting `tests/test_runtime.py` and `tests/test_anthropic.py` response fixtures. Its context-managed public test surface is `runtime`, `agent`, `policy`, `requests`, `queue_text(text)`, `queue_calls(calls)`, `queue_http_failure(status)`, `add_tool(name, handler, *, authorize=None, operation_key=None)`, and `queue_after_tool(text)`. Responses use each provider's real wire format; helpers count actual POSTs and keep finite server/subprocess shutdown deadlines. Export pytest factory `turn_fixture` in the new test modules through a local fixture plugin.

- [ ] Add the role/correction regression for both transports:

```python
@pytest.mark.parametrize("provider", ["openai-compatible", "anthropic"])
def test_failed_correction_is_in_next_payload(turn_fixture, provider):
    with turn_fixture(provider) as fx:
        fx.queue_text("Prague")
        history = (ConversationMessage.from_dict({"role": "user", "content": [
            {"type": "text", "text": "I meant Prague"}], "turnStatus": "failed",
            "failureCode": "providerFailure"}),)
        result = fx.runtime.turn(fx.agent, message="Which city?", history=history, policy=fx.policy)
        assert result.result.stdout == "Prague"
        assert "I meant Prague" in json.dumps(fx.requests[0])
        assert "failed" in json.dumps(fx.requests[0])
```

- [ ] Observe RED with `.venv/bin/python -m pytest tests/test_conversation_turns.py -q`.
- [ ] Route tool-free and tool-using turns through the shared loop. Build privileged instructions from approved body/resources, then encode neutral history and current user/input_data with unprivileged roles. Preserve ordered blocks and native request/result pairing; store no provider continuation in public records. Default workspace-none must disable transcript file creation as well as file tools.
- [ ] Prove repeated native IDs across turns, consecutive users, ordered multi-tool blocks, streaming previews and private-provider-field exclusion. Run the new tests and existing `test_provider_execution.py`, `test_anthropic.py`, `test_anthropic_stream.py`; review and commit.

### Task 3: Strict whole-batch tool admission and explicit workspace

**Files:** Modify `openai_compatible.py`, `anthropic.py`, `tool_registry.py`, `tools.py`, `runtime.py`; create `tests/test_conversation_tool_batches.py`.

**Interfaces:** `BoundedToolRegistry.validate_batch(calls)` returns an ordered tuple of validated requests without handlers, authorizers, operation-key resolution, journal access, counters or events. It reuses bounded `parse_arguments()` and static schemas. Add a disabled builtin registry for workspace-none; do not construct `Path(None)` or substitute `'.'`.

- [ ] Write a partly invalid batch regression and observe RED:

```python
def test_second_invalid_call_prevents_first_effect(turn_fixture):
    with turn_fixture("openai-compatible") as fx:
        seen = []
        fx.add_tool("lookup", lambda args: seen.append(args) or {})
        fx.queue_calls([{"id": "a", "name": "lookup", "arguments": "{}"},
                        {"id": "b", "name": "not_granted", "arguments": "{}"}])
        result = fx.runtime.turn(fx.agent, message="Search", policy=fx.policy)
        assert result.result.metadata["failure_reason"] == "invalidToolRequest"
        assert seen == []
```

- [ ] In turn mode preserve malformed JSON/SSE call shape before normalization. Do not synthesize absent IDs, coerce arguments to `{}`, drop unknown fields or discard malformed calls. Validate the entire batch before entering sequential execution; valid batches still obey per-tool budget checks individually.
- [ ] Use strict duplicate-key/nonfinite parsing for native argument objects and provider envelopes in turn mode even when no reported-token cap is configured; compatibility parsing cannot erase invalid evidence before batch admission.

```python
validated = registry.validate_batch(raw_calls)  # all-or-nothing structural admission
for call in validated:
    registry.execute_message(call)  # validated standard call dict; preserve ID/context/budget/journal boundary
```

- [ ] Test unknown/ungranted names, malformed IDs/fields, duplicate IDs/argument keys, nonfinite and schema-invalid arguments on JSON and fragmented SSE for both providers. A tool-free binding with max_tool_calls=0 must produce zero handler/authorizer/resolver/journal calls even for a fabricated native request.
- [ ] Test no workspace for native callbacks; missing explicit workspace/roots fails file/CLI grants before provider dispatch. Keep trusted CLI preparation probes a documented explicit effect, outside pure preparation. Verify legacy correction and existing CLI behavior with new batch tests plus custom-tool/CLI suites; review and commit.

### Task 4: Actual input bounds and compaction suppression

**Files:** Modify `execution.py`, `runtime.py`, `conversation_preparation.py`, provider encoding methods; create `tests/test_conversation_bounds.py`.

**Interfaces:** Produce `contextLimit` and output/record failure codes while preserving complete failed user input. Actual native request encoding is measured once with the same serializer used for transport, before each dispatch.

- [ ] Write a real compactor activation regression. Use nine outcomes, one older successful JSON body of 100,000 characters outside the latest four; enlarge host tool/record/request limits. Assert endpoint compaction settings cannot change turn content while legacy `run()` activates its existing compactor.

```python
def test_turn_preserves_compactor_fixture(turn_fixture):
    with turn_fixture("openai-compatible") as fx:
        history = fx.compaction_history(outcomes=9, older_result_chars=100_000)
        fx.queue_text("done")
        before = json.dumps([m.to_dict() for m in history])
        result = fx.runtime.turn(fx.agent, message="Continue", history=history,
                                 policy=fx.large_policy, limits=fx.large_limits)
        assert result.result.exit_code == 0
        assert "x" * 100_000 in json.dumps(fx.requests[0])
        assert json.dumps([m.to_dict() for m in history]) == before
```

Add `compaction_history`, `large_policy`, `large_limits` to the declared fixture in this task; fixture returns typed messages and explicitly raises tool/record/input limits while retaining finite work bounds.
- [ ] Observe RED, then use a per-invocation `compact_tool_results=False` option in the shared loop. Never mutate endpoint features. Recheck native encoded payload after each result, including current-turn tool outcomes.
- [ ] Test first-request overflow produces zero POSTs; post-tool overflow produces no follow-up POST and retains prior usage/effect. Test independent user/final/complete-record caps, escaped Unicode and stricter policy/transport limits. Provider window rejection remains explicit, with no shrinking retry.
- [ ] Run bounds tests plus existing `test_input_boundary.py` and compaction tests; review and commit.

### Task 5: Failure evidence and public result sanitization

**Files:** Create `src/prosaic_runtime/turn_capture.py`, `tests/test_conversation_failure_evidence.py`; modify `runtime.py`, `tools.py`, `accounting_capture.py`, `execution.py`.

**Interfaces:** `TurnCapture.record_provider_observation`, `record_tool_completion`, `finish(result)->TurnResult` retain bounded observations outside catch branches. Existing recorder schema/serialization does not change.

- [ ] Add a journal-complete/cancellation regression using existing `tests/support/tool_journal.py`; add a recorder `observe()` failure regression using the real loopback fixture. Observe RED in new failure tests.

```python
def test_post_commit_cancel_retains_effect(turn_fixture):
    with turn_fixture("openai-compatible") as fx:
        fx.cancel_after_journal_commit()
        result = fx.run_journaled_turn()
        evidence = result.evidence.to_dict()
        assert evidence["toolOutcomes"][0]["state"] == "completed"
        assert result.completed_turn is None
        assert fx.handler_calls == 1
```

This task adds `cancel_after_journal_commit`, `run_journaled_turn`, `handler_calls` to the fixture using public tool/journal/cancellation hooks, not patched engine methods.
- [ ] Record trustworthy journal completion/replay immediately before the final cancellation/deadline check. Preserve provider usage in an in-memory snapshot before `recorder.observe()` can raise. Capture attempted-call uncertainty even without a token cap; retain known prior totals. Finalize bounded failure evidence for accounting errors, critical hooks, output limits and context overflow.
- [ ] Apply the metadata/stderr allowlist above and return no completed assistant group after failure. Do not label an unknown journal commit as completed or synthesize billing-eligible evidence.
- [ ] If full tool/result data exceed the evidence cap, retain a fixed bounded failure summary containing observed counters, known/unknown usage and each completed/uncertain tool's opaque invocation/index/signature/status reference. Large raw bodies may be omitted; known effect status must never become unknown merely because display data overflow. Reserve this terminal summary before dispatch and never claim an unknown journal commit is known.
- [ ] Test missing/conflicting usage, prepare/final accounting failures, post-effect cancellation/deadline, journal lost acknowledgements and uncertainty, oversized raw errors/results, observer failures and partial streams. Run new failures plus accounting/budget/effect/telemetry suites, then full `.venv/bin/python -m pytest`; review and commit.

### Task 6: Installed documentation and Runtime release candidate

**Files:** Create `docs/conversations.md`, `scripts/conversation_wheel_smoke.py`; modify `README.md`, `pyproject.toml`, `src/prosaic_runtime/__init__.py`, `adapters/postgres/pyproject.toml`, `adapters/postgres/src/prosaic_runtime_postgres/__init__.py`, `tests/test_distribution_metadata.py`, `tests/test_package_compatibility.py`, `scripts/production_wheel_smoke.py`, `scripts/release_artifacts.py`, `.github/workflows/test.yml`, `.github/workflows/publish.yml`, `docs/package-index.md` and recorder README.

**Interfaces:** Export `conversation_turn_v1`, public records and methods. Candidate versions: Runtime 0.9.0; recorder 0.3.0 depending on `b3-prosaic-runtime>=0.9,<0.10`. Core remains 0.4.1. Recorder data/schema are unchanged; this adapter version qualifies the new Runtime family rather than claiming untested cross-family support.

- [ ] Write installed-consumer tests rejecting sibling-source imports and asserting public capability, pure preparation and a two-turn synthetic exchange. Observe the missing wheel smoke/capability failure.
- [ ] Document complete-history limits, tool-free interpretation, no workspace default, failed corrections, provisional streaming, metadata privacy, finite policies and stateless Runtime. Include an inspected `ProsaicArtifact` example, not an invented raw-prompt entrypoint.
- [ ] Run full Runtime suite and `.venv/bin/python -m build`, then `.venv/bin/python -m build adapters/postgres`. Run existing wheel/production smoke plus new conversation smoke from a fresh installed environment with dependency resolution and `pip check`; preserve original release receipts.
- [ ] Qualify existing Python/platform/sandbox lanes and recorder PostgreSQL 16/18 Linux AMD64/ARM64. The recorder lane uses owned `METERING_TEST_DSN`/`METERING_RUNTIME_DSN`; missing required infrastructure cannot count as a passing lane. No live provider calls are necessary here.
- [ ] Independently review the whole Runtime change against the spec. Save exact source/wheel/sdist hashes and candidate lineage for Harness; commit only after verified checks. Publish through the existing qualified workflow after the linked release gate permits it.

## Self-review and handoff

All six tasks include a failing behavior test, a targeted check, an implementation boundary and a local review/commit. Read the spec and this plan together. Harness consumes public `PreparedTurn`, `ConversationMessage`, `TurnResult` and capabilities; it never estimates provider payloads itself. The cross-repository sequence and platform/publication gates are in the Harness conversation implementation plan.
