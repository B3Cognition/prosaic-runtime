# Token-aware Context Assembly Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for the established native execution method. Use superpowers:subagent-driven-development only if the user changes that preference. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship optional token-aware conversation admission and oldest-complete-turn omission as reusable SDK capabilities, with durable recovery and a synthetic Integration Lab demonstration.

**Architecture:** Runtime owns native request construction, counter admission, deterministic fitting and continuation checks. Harness owns frozen bindings, attempt resource checkpoints, bounded version-2 records and recovery; stores negotiate support before session creation. Integration Lab consumes installed, checksummed SDK wheels and exposes filtered reports, while provider usage and Lago billing remain separate.

**Tech Stack:** Existing Python SDKs, pytest, existing OpenAI-compatible/Anthropic adapters, synchronous SessionStore APIs, PostgreSQL 16/18, FastAPI and the existing chat UI. No new compulsory tokenizer dependency.

**Spec:** [Approved design](../specs/2026-10-14-context-assembly-design.md), approved after local Runtime commit `33ecfcc`.

## Global Constraints

- Full history with explicit rejection remains the default. Stored history is never truncated.
- System instructions, the current request and granted tool schemas are mandatory. No model-generated summaries are introduced.
- Cost reservations/degradation and trajectory evaluation are separate increments.
- Byte, JSON, history and receipt limits remain independent safety ceilings.
- With both policies absent, preparation/binding identities and serialized contracts remain unchanged.
- Separate `context_policy` and `interpretation_context_policy`; there is no implicit inheritance between the two policies.
- Policy serialization and reports use camelCase; public Python attributes use snake_case.
- `maxCandidateChecks`: at least 2, default 128, maximum 256.
- `maxAssemblyBytes`: default 33,554,432 bytes, maximum 268,435,456 bytes.
- `assemblyTimeoutMs`: default 2,000 ms, maximum 30,000 ms; enclosing execution deadline also applies.
- Version-1 report and counter descriptor: at most 16,384 encoded bytes and 512 nodes each. Selection descriptor: 8,192 bytes and 256 nodes. Attempt resource state: 4,096 bytes and 128 nodes.
- Opted-in sessions use session/receipt version 2 and Runtime encoding `conversation-turn-v2`, with version-1 assembly reports and negotiated `context_assembly_v1`.
- First release includes a counter interface and synthetic demonstration, not a qualified production counter. Host counters must execute locally without network I/O or inference.
- Provider usage is accounting evidence; Lago receives billing events separately and does not fit context.
- Use public installed APIs in Lab, checksummed candidate wheels during qualification, and published wheels for released adoption. Required lanes fail rather than skip missing infrastructure.
- Preserve unrelated edits, historical evidence, active demonstrations and version-1 data. Use `apply_patch`; do not modify Core or Runtime PostgreSQL accounting storage for this feature.

## Review Focus

1. A valid nonmonotonic conservative counter must not cause premature mandatory-only rejection; Task 2 tests full-history-first selection.
2. A counter mutating its argument, returning `bool`, or raising an exception containing prompt text must not mutate a sealed request or leak that text; Tasks 1–3 test owned copies and closed errors.
3. A lease loss or unknown checkpoint acknowledgement must prevent dispatch even if the callback finished; Tasks 5–7 test fencing, durable charge reservations and marker ordering.
4. An opted-in interpreter with a legacy ordinary endpoint must retain zero-tool authority and correct record encoding; Tasks 4 and 7 test both asymmetric policy combinations.
5. A long provider wait must not spend assembly active-time allowance; a long counter must still fail cooperatively after returning; Tasks 2–3 exercise both with a deterministic clock.

## Repository map and execution order

Paths below are relative to the named repository, all siblings under `prosaics/`.
Create managed isolated worktrees at execution time using the worktree skill;
carry this approved specification and plan into the Runtime worktree. Reuse
suitable attached worktrees first. Do not copy unrelated examples or Lab changes
into feature commits. The Lab has existing uncommitted adoption/UI changes:
inspect their diff and preserve them before starting; stage only feature hunks.

These are dependent deliverables, so use one coordinated plan rather than
independent subsystem plans. Runtime Tasks 1–3 produce a usable standalone SDK
increment. Harness Tasks 4–7 enable durable use, and Lab Tasks 8–9 qualify adoption.

| Task | Owner | Responsibility |
|---|---|---|
| 1 | Runtime | Immutable public policy/counter/report/selection/work contracts |
| 2 | Runtime | Pure bounded full-history-first assembly |
| 3 | Runtime | Execution readmission, final wire gates and failure evidence |
| 4 | Harness | Per-purpose bindings and capability negotiation |
| 5 | Harness | Version-2 reducer, bounded reservations and resource checkpoints |
| 6 | Harness + its PostgreSQL adapter | Explicit storage negotiation/migration |
| 7 | Harness | Marker ordering, recovery and workflow interpretation |
| 8 | Lab | Synthetic host counter, report projection, UI and acceptance scenarios |
| 9 | SDKs + Lab | Installed-wheel qualification and release/adoption handoff |

Each task runs its regression first, confirms the expected failure, implements
the described change, reruns its focused tests, then commits only its files.
Do not bypass a required test failure with a skip or a weakened assertion.

## Shared public interface decisions

Task 1 defines these names, which later tasks must use without renaming:

```python
@dataclass(frozen=True)
class ContextPolicy:
    mode: str
    context_window_tokens: int
    reserved_output_tokens: int
    safety_margin_tokens: int
    counter_identity: str
    max_candidate_checks: int = 128
    max_assembly_bytes: int = 33_554_432
    assembly_timeout_ms: int = 2_000

@dataclass(frozen=True)
class TokenCounterDescriptor:
    identity: str
    provider: str
    model: str
    framing_revision: str
    context_capacity_tokens: int
    implementation_version: str
    tokenizer_assets_digest: str
    template_assets_digest: str
    counting_kind: str  # exact or conservativeUpperBound
    guarantee: str     # bounded, host-authored qualification description

class TokenCounter(Protocol):
    def count_tokens(self, request: dict[str, object]) -> int: ...

@dataclass(frozen=True)
class TokenCounterRegistration:
    descriptor: TokenCounterDescriptor
    counter: TokenCounter

```

`TokenCounterRegistry` takes
`Mapping[str, TokenCounterRegistration]` in its constructor and exposes
`resolve(identity: str) -> TokenCounterRegistration` over an immutable snapshot.
The Protocol ellipsis is its Python declaration, not deferred implementation.
Policy/descriptor/report/selection/work objects expose validated `to_dict()` and
`from_dict()` using owned encoded JSON. Reject unknown fields, nonfinite values,
booleans in integer fields, invalid UTF-8, cycles, excess depth and invalid digests.
Counters return an integer from 0 through `2**63 - 1`, never a heuristic estimate.
Registry snapshots own descriptors and callback references; callbacks are trusted,
not serialized. Snapshot endpoint/framing/digests before invoking host code.

`ContextAssemblyReport` owns these closed fields: `version`, `mode`,
`counterIdentity`, `sourceHistoryDigest`, `selectedHistoryDigest`, `inputTokens`,
`countingKind`, `reservedOutputTokens`, `safetyMarginTokens`, `inputCapacityTokens`,
`encodedInputBytes`, `retainedTurnCount`, `omittedTurnCount`, `retainedRange`,
`omittedRange`, `candidateChecks`, `candidateBytes`. Ranges contain
`startTurn` and `endTurnExclusive`, use one-based positions, and may be `null`
when empty. They are compact ranges, never arrays of message IDs.

`ContextSelection` owns `version`, `contextPolicyIdentity`, `counterDescriptorIdentity`,
`sourceHistoryDigest`, `selectedHistoryDigest`, `retainedRange`, `omittedRange`,
`initialReportIdentity`. Policy, descriptor and report are separately bounded
objects, referenced by digest rather than nested into the smaller selection
envelope. It is data for trusted host reconstruction, not an executable
deserializer. Report identity is immutable after preparation.

`ContextWorkState` owns `version`, `candidateChecks`, `candidateBytes`,
`activeAssemblyMicros`, `reservedActiveMicros`, `readmissionCandidateBytes`,
`readmissionPending`, `checkpointSequence`. All counters are bounded integers;
no timing values participate in context identity. Resource state remains mutable
only through new immutable snapshots and validated reducer transitions.

Tasks 2–3 extend the existing Runtime methods without changing absent-policy calls:

```text
prepare_turn(artifact, *, message, history=(), input_data=None, policy=None,
             limits=None, workspace=None, context_policy=None,
             token_counters=None, context_selection=None, context_report=None,
             context_work=None)

turn(artifact, *, message=None, history=(), input_data=None, policy=None,
     limits=None, workspace=None, context_policy=None, token_counters=None,
     context_work=None, context_checkpoint=None, on_admitted=None,
     on_event=None, cancelled=None, env=None, context=None, accounting=None,
     observer=None, operation_context=None, tool_journal=None)
```

`context_selection` requires its separately supplied `context_report`, an explicit
context policy and matching source, descriptor and report identities.
Reconstruction rebuilds the frozen suffix without
performing a new omission search; execution still recounts before admission.
Prepared execution cannot override its policy or selection. `token_counters`
supplies the matching local implementations, and `context_work` restores durable
resource state rather than replacing immutable context material.
`PreparedTurn.context_report`, `.context_selection` and `.context_work` are
owned public values, `None` for legacy preparation. `TurnResult.context_report`
and `.context_work` are optional bounded final aggregate values; legacy evidence
documents receive no new fields.

`context_checkpoint(state: ContextWorkState) -> None` persists a resource
transition before a callback is entered and reconciles after it returns.
`on_admitted() -> None` runs once after frozen-selection readmission, all static
effect/accounting admission and cancellation checks, and before invocation
observation, accounting intent or provider/tool effects. Harness writes its
dispatch marker here. Failure of either hook prevents dispatch; an unknown
storage acknowledgement cannot grant permission to continue. Continuation
checkpoints occur within the already dispatched attempt.

### Task 1: Immutable optional contracts and counter admission

**Repository:** Runtime.

**Files:** Create `src/prosaic_runtime/context_contracts.py`,
`src/prosaic_runtime/context_counters.py`, `tests/support/context_counter.py`,
`tests/test_context_contracts.py`. Modify `src/prosaic_runtime/__init__.py`.

**Interfaces:** Produce the public contracts above, with validated JSON factories.
`TokenCounterRegistry.resolve(identity)` returns a registration or raises
`TurnAdmissionError('invalidPolicy')`. Internal
`count_owned(registration, native_request) -> int` passes an owned bounded copy,
checks the result and replaces callback exceptions with a safe error.

- [ ] **Step 1:** Write export, roundtrip, ownership, integer-bound, capacity,
  descriptor-size/node, unknown-field and secret-exception regressions. Add this
  concrete reusable synthetic helper to `tests/support/context_counter.py`:

```python
import json
from prosaic_runtime import (ContextPolicy, TokenCounterDescriptor,
    TokenCounterRegistration, TokenCounterRegistry)

def fixture_context(provider='openai-compatible', *, model='fixture-model',
                    mode='fullHistory', capacity=4096, reserve=128,
                    count=None, checks=128, byte_allowance=33_554_432,
                    timeout_ms=2000):
    class Counter:
        def count_tokens(self, request):
            return count(request) if count is not None else len(
                json.dumps(request, allow_nan=False).encode('utf-8'))
    descriptor = TokenCounterDescriptor(
        identity='synthetic-byte-v1', provider=provider, model=model,
        framing_revision={'openai-compatible': 'openai-chat-v1',
                          'anthropic': 'anthropic-messages-v1'}[provider],
        context_capacity_tokens=1_048_576,
        implementation_version='1', tokenizer_assets_digest='0'*64,
        template_assets_digest='1'*64, counting_kind='exact',
        guarantee='Exact byte units in the synthetic fixture grammar only.')
    registry = TokenCounterRegistry({descriptor.identity:
        TokenCounterRegistration(descriptor, Counter())})
    policy = ContextPolicy(mode, capacity, reserve, 0, descriptor.identity,
                           checks, byte_allowance, timeout_ms)
    return policy, registry
```

  This helper's units certify only the synthetic grammar; never describe them as
  production model tokens. Representative negative test:

```python
@pytest.mark.parametrize('bad', [True, -1, 1.5, 2**63])
def test_counter_result_is_closed(bad):
    from prosaic_runtime.context_counters import count_owned
    policy, registry = fixture_context(count=lambda request: bad)
    with pytest.raises(TurnAdmissionError) as caught:
        count_owned(registry.resolve(policy.counter_identity), {'messages': []})
    assert caught.value.code == 'invalidPolicy'
```

- [ ] **Step 2:** Run `.venv/bin/python -m pytest tests/test_context_contracts.py -q`;
  verify failure names a missing export or new behavior, not broken fixture setup.
- [ ] **Step 3:** Implement frozen contracts with the existing `encode_owned`
  traversal. Serialize policy fields in camelCase, validate reserve plus margin
  strictly below capacity, enforce descriptor endpoint/model/framing matches,
  and copy registry keys/descriptors before storing them. Test that clearing the
  caller's original registration mapping cannot alter the snapshot.
- [ ] **Step 4:** Rerun the contract file plus
  `tests/test_conversation_contracts.py`. Test a mutating counter against two
  independently decoded requests; the original request must remain unchanged.
- [ ] **Step 5:** Commit the four new files and export changes:
  `feat: add optional context policy and trusted counter contracts`.

### Task 2: Bounded deterministic assembly and immutable preparation

**Repository:** Runtime.

**Files:** Create `src/prosaic_runtime/context_assembly.py`,
`src/prosaic_runtime/context_work.py`, `tests/test_context_assembly.py`.
Modify `src/prosaic_runtime/conversation_preparation.py`,
`src/prosaic_runtime/conversation.py`, `src/prosaic_runtime/runtime.py`.

**Interfaces:** Consume Task 1 contracts and existing native `initial_payload`.
Produce `assemble_context(source_history, build_candidate, policy, registration,
limits, *, work=None, clock=time.monotonic_ns)` returning selected owned history,
report, selection and work state. `build_candidate(history)` returns the exact
native dict after output reservation enforcement. Internal `ContextWorkBudget`
owns policy/state; `construct(callback)` measures active construction time;
`check_candidate(encoded_bytes, callback=None)` charges counts/bytes and measures
active counting time, returning `None` when no counter is called.
`reserve_readmission(encoded_bytes)` requires an available check, bytes and
positive active time before sealing. Clock injection is internal.

- [ ] **Step 1:** Add real preparation tests using the existing
  `support.conversation_server.turn_fixture` and Task 1 helper:

```python
def test_actual_full_history_can_fit_nonmonotonic_counter(turn_fixture):
    from prosaic_runtime import ConversationMessage
    history = (ConversationMessage.from_dict({'role': 'user',
        'content': [{'type': 'text', 'text': 'retained-marker'}],
        'turnStatus': 'failed', 'failureCode': 'providerFailure'}),)
    policy, counters = fixture_context(mode='omitOldestTurns', capacity=200,
        reserve=20, count=lambda request:
            10 if 'retained-marker' in json.dumps(request) else 1000)
    with turn_fixture('openai-compatible') as fx:
        prepared = fx.runtime.prepare_turn(fx.agent, message='now',
            history=history, policy=fx.policy, context_policy=policy,
            token_counters=counters)
        assert prepared.context_report.to_dict()['omittedTurnCount'] == 0
        assert fx.requests == []
```

  Add exact-boundary and one-token-overflow cases, both providers, UTF-8/schema
  overhead, native output cap, failed-turn grouping and complete tool pairs.
  Test provider-input byte overflow rescued by omission while source limits pass,
  and source-storage overflow rejected before any counter/provider invocation.
  Set `checks=2` with a first candidate that fails: omission requiring another
  count plus readmission must reject rather than return unusable preparation.
- [ ] **Step 2:** Run `.venv/bin/python -m pytest tests/test_context_assembly.py -q`.
- [ ] **Step 3:** Implement validation-before-grouping and oldest-first suffix
  search. For each candidate, reconstruct native framing and enforce output cap;
  use `min(endpoint.max_tokens, reserved_output_tokens)` when an endpoint cap
  exists, otherwise set the reserved cap. Pin framing revisions to
  `openai-chat-v1` and `anthropic-messages-v1`; reject unrecognized alternative
  output controls rather than assuming they enforce the same ceiling.
  charge construction work even if provider byte overflow avoids the counter.
  Never subtract message counts or use binary search. Stop at the first actual
  candidate fitting both limits. Reject an exhausted search/work allowance with
  a closed `contextLimit` and fixed limit field.

```python
start = 0
while True:
    def construct():
        native = build_candidate(source_history[start:])
        size = len(json.dumps(native, allow_nan=False).encode('utf-8'))
        return native, size
    native, encoded_bytes = budget.construct(construct)
    callback = (lambda: count_owned(registration, native)) if encoded_bytes <= input_byte_cap else None
    tokens = budget.check_candidate(encoded_bytes, callback)
    if tokens is not None and tokens <= input_capacity:
        budget.reserve_readmission(encoded_bytes)
        return seal_selection(start, native, tokens, budget.state)
    if policy.mode == 'fullHistory' or start == len(source_history):
        raise TurnAdmissionError('contextLimit',
            'maxInputBytes' if tokens is None else 'contextWindowTokens')
    start = next_turn_start(source_history, start)
```

  Define `next_turn_start` in this module by scanning to the next validated user
  message; define `seal_selection` there to build Task 1 records/digests. The
  over-byte branch still debits one candidate and its bytes using the same budget
  without calling `count_owned`. Construction and final encoding are both inside
  the measured scope. No report contains request text.
- [ ] **Step 4:** Extend preparation's opt-in branch and seal v2 material with
  full source identity, selected identity, policy/descriptor and report. Keep v1
  branch/material unchanged. Reconstruct `context_selection` without search or
  counting; validate its frozen identities and restore `context_work`. Exclude
  resource timing from identity. Add deterministic clock tests for cooperative
  timeout and cumulative bytes, including candidates skipped for provider bytes.
- [ ] **Step 5:** Run `.venv/bin/python -m pytest tests/test_context_assembly.py
  tests/test_context_contracts.py tests/test_conversation_bounds.py
  tests/test_conversation_contracts.py -q`; commit
  `feat: fit conversation context with bounded whole-turn omission`.

### Task 3: Readmission, exact wire guards and post-effect failure evidence

**Repository:** Runtime.

**Files:** Modify `src/prosaic_runtime/runtime.py`,
`src/prosaic_runtime/execution.py`, `src/prosaic_runtime/openai_compatible.py`,
`src/prosaic_runtime/anthropic.py`, `src/prosaic_runtime/turn_capture.py`,
`src/prosaic_runtime/conversation.py`, `src/prosaic_runtime/context_work.py`.
Create `src/prosaic_runtime/context_execution.py`,
`tests/test_context_execution.py`. Extend `scripts/conversation_wheel_smoke.py`
and `docs/conversations.md`.

**Interfaces:** Produce internal `ContextExecutionGuard` with
`readmit(prepared)`, `check_wire(native, *, deadline)`, and bounded final
`report`/`work` snapshots. Consume the checkpoint and admission hooks defined
above. Advertise `context_assembly_v1` only once Tasks 1–3 work end to end.

- [ ] **Step 1:** Write two-provider transport tests that capture exactly what the
  counter receives and compare it with each actual wire request. Readmission
  charges once; the first wire gate verifies the pre-counted native digest rather
  than debiting/counting it again. Subsequent requests must each count their
  actual native payload, including provider framing and output control.

```python
def test_context_exhaustion_after_tool_keeps_completed_effect(turn_fixture):
    effects = []
    policy, counters = fixture_context(capacity=200, reserve=20,
        count=lambda request: 1000 if 'large-tool-result' in json.dumps(request) else 10)
    with turn_fixture('openai-compatible') as fx:
        fx.add_tool('lookup', lambda arguments:
            effects.append('completed') or {'value': 'large-tool-result'})
        fx.queue_calls([{'id': 'call_0', 'name': 'lookup', 'arguments': '{}'}])
        result = fx.runtime.turn(fx.agent, message='lookup', policy=fx.policy,
            context_policy=policy, token_counters=counters)
        assert len(fx.requests) == 1
        assert effects == ['completed']
        assert result.result.exit_code >= 0
        assert result.completed_turn is None
        assert result.evidence.to_dict()['failureCode'] == 'contextLimit'
        assert result.evidence.to_dict()['providerAttempts'] == 1
```

  Extend this test with a real synthetic journal to assert recorded completion
  and retained usage; add counter exception after a tool, uncertain effect and
  journal-failure precedence, streaming on/off, timeout/cancellation classification.
- [ ] **Step 2:** Run `.venv/bin/python -m pytest tests/test_context_execution.py -q`.
- [ ] **Step 3:** Perform v2 readmission once, persisting reserved charges before
  callback invocation. Reserve remaining active microseconds while inside the
  callback; reconcile to measured active time only on successful checkpoint.
  The same active-time reservation brackets candidate construction/encoding:
  checkpoint before entering it and reconcile only after it completes, so a
  construction crash cannot silently replenish time during recovery.
  Interrupted callbacks retain that reservation. Invoke `on_admitted` only after
  all static checks and before observation/accounting. Keep legacy turns without
  either hook unchanged.
  At both transport boundaries, guard the actual native object before usage
  accounting intent and HTTP. Snapshot the exact encoded bytes/digest and reuse
  those bytes for dispatch. A later payload mutation is `bindingMismatch`.
  Attach guard invocation to accounted transport wrappers before their intent
  allocation, not only inside the base HTTP method after intent was recorded.
- [ ] **Step 4:** Convert continuation guard failures to bounded failed `Result`
  objects before `TurnCapture.finish`, with empty answer, no completed group and
  retained accounting/tool evidence. Use `contextLimit` for resource/capacity
  failures and `bindingMismatch` for counter/configuration failures; preserve
  stronger tool uncertainty and existing timeout/cancellation. Add checkpoint
  ordering test: reservation, callback, reconciliation, admission, provider.
  Counter exceptions/negative results cannot expose their messages in safe output.
  Use a fake clock/provider fixture to prove provider waiting is excluded.
- [ ] **Step 5:** Run focused context tests and existing conversation failure,
  tool-batch and distribution tests. Run the complete Runtime suite once this
  increment is complete: `.venv/bin/python -m pytest`. Commit
  `feat: enforce context limits through turn execution and continuations`.

### Task 4: Separate binding policies and explicit capability negotiation

**Repository:** Harness.

**Files:** Modify `src/prosaic_harness/conversation_bindings.py`,
`src/prosaic_harness/session_store.py`, `src/prosaic_harness/__init__.py`.
Create `tests/support/context_counter.py`, `tests/test_context_bindings.py`; extend
`tests/test_conversation_bindings.py`.

**Interfaces:** Extend `ConversationBindings.__init__` with optional
`context_policy=None`, `interpretation_context_policy=None`, `token_counters=None`.
Expose read-only same-named policy/registry properties, plus
`context_enabled: bool`. Add optional `SessionStore.capabilities` negotiation:
`getattr(store, 'capabilities', frozenset())` allows legacy stores only for v1.
Select each policy against its own resolved ordinary/interpreter endpoint.

- [ ] **Step 1:** Capture legacy binding identity/document goldens using existing
  `binding_factory`; keep their exact serialized bytes and hash in a fixture.
  Add this rejection test and tests for two different provider/model descriptors:

```python
def test_interpreter_context_cannot_exist_without_interpreter(binding_factory):
    policy, counters = fixture_context(model='synthetic')
    with pytest.raises(ConversationError) as caught:
        binding_factory(interpretation_context_policy=policy,
                        token_counters=counters)
    assert caught.value.code == 'bindingMismatch'
```

  Copy only the small trusted synthetic helper from Task 1 into
  `tests/support/context_counter.py` in Harness; this is test code, not a SDK
  engine. Test ordinary-only and interpreter-only configurations, no inheritance,
  capacity beyond the descriptor, mutable registry ownership and model digest drift.
- [ ] **Step 2:** Run `.venv/bin/python -m pytest tests/test_context_bindings.py
  tests/test_conversation_bindings.py -q` against the built Runtime candidate.
- [ ] **Step 3:** Serialize only explicit policies/selected descriptors. Preserve
  the exact old identity dictionary when both absent. For opted-in bindings,
  require Runtime `context_assembly_v1` and freeze both endpoint descriptors;
  `readmit()` resolves the same snapshots. Do not serialize executable callbacks.
  Keep existing `ConversationPolicy.context_mode == 'fullHistory'` validation
  unchanged; omission mode belongs to the new optional Runtime context policy.
- [ ] **Step 4:** Add negotiation tests for missing Runtime/store capability and
  a legacy store with no capabilities attribute. No token counter is required
  on the absent-policy path. Rerun focused binding tests.
- [ ] **Step 5:** Commit `feat: bind separate conversation context policies`.

### Task 5: Version-2 reducers, capacity reservations and durable resource state

**Repository:** Harness.

**Files:** Create `src/prosaic_harness/session_context.py`,
`tests/test_session_context.py`. Modify
`src/prosaic_harness/session_contracts.py`, `src/prosaic_harness/session_codec.py`.
Extend `tests/test_conversation_limits.py` and `tests/test_session_store.py`.

**Interfaces:** Existing v1 functions remain callable unchanged. V2 dispatch
routes through focused helpers in `session_context.py` rather than duplicating
the whole reducer. Add `checkpoint_context_work(document, invocation_id,
work: ContextWorkState) -> dict`, `context_reservation_bounds() -> tuple[int,int]`
and closed v2 context validators. Extend `reserve_attempt` with optional
`context_selection=None`, `context_report=None`, `context_work=None`;
`make_receipt` reads optional
final `TurnResult` context fields. `new_session` chooses v2 from binding opt-in.

- [ ] **Step 1:** Freeze legacy sealed session/receipt bytes and transition hashes
  before altering the codec. Use existing `session_document` fixtures to prove
  v1 roundtrips remain byte-identical. Add v2 source/projection mismatch,
  unknown field, report-size/node and unsafe timing-identity tests.
  Define a maximum-size reservation fixture using the allowed object ceilings,
  including containing field names, JSON escapes, both purpose descriptors,
  report/selection digests and final failure material; count nodes independently.

```python
def test_context_work_cannot_be_reset(session_document):
    from prosaic_harness.session_context import validate_work_transition
    from prosaic_runtime import ContextWorkState
    before = ContextWorkState.from_dict({'version': 1, 'candidateChecks': 2,
        'candidateBytes': 300, 'activeAssemblyMicros': 10,
        'reservedActiveMicros': 0, 'readmissionCandidateBytes': 150,
        'readmissionPending': True, 'checkpointSequence': 2})
    after = ContextWorkState.from_dict({**before.to_dict(),
        'candidateChecks': 0, 'checkpointSequence': 3})
    with pytest.raises(ConversationError):
        validate_work_transition(before, after)
```

  Implement `validate_work_transition` in `session_context.py`; count/bytes and
  consumed time never decrease. Only a matching completion transition may
  release an outstanding active-time reservation; checkpoints advance by one.
- [ ] **Step 2:** Run `.venv/bin/python -m pytest tests/test_session_context.py -q`.
- [ ] **Step 3:** Implement closed v2 records with `contextSelection`,
  `initialContextReport`, `contextWork` in context-enabled attempts and
  `finalContextReport`/final work in receipts. An absent-purpose policy uses its
  legacy Runtime encoding within the v2 session/receipt envelope; validators
  check purpose policy rather than requiring v2 Runtime encoding unconditionally.
  Keep source history digest separate from selected projection. Reject in-place
  v1-to-v2 transitions and unsupported cross-version receipt adoption.
- [ ] **Step 4:** Compute reservation overhead from bounded encoded objects,
  including all enclosing field/digest overhead, with per-turn/session/receipt
  byte and node debits. Admission reserves initial/final reports and failure
  receipts before effect dispatch, including bounded work checkpoint state.
  Test at exact capacity and one byte/node short; no attempts/accounting/provider
  effect should be allocated on initial rejection. Test failed allocated
  readmission receipts with zero provider/tool attempts and no dispatch marker.
  They must have closed nonnegative failure results and cannot imply an effect.
- [ ] **Step 5:** Run session codec/store/limit tests and commit
  `feat: persist bounded context selections and work reservations`.

### Task 6: Storage capability, file compatibility and explicit PostgreSQL migration

**Repository:** Harness, including `adapters/postgres`.

**Files:** Modify `src/prosaic_harness/memory_session_store.py`,
`src/prosaic_harness/file_session_store.py`,
`adapters/postgres/src/prosaic_harness_postgres/session_schema.py`,
`adapters/postgres/src/prosaic_harness_postgres/session_store.py`,
`adapters/postgres/src/prosaic_harness_postgres/cli.py`.
Create `tests/test_context_file_store.py`,
`adapters/postgres/tests/test_session_context.py`.
Extend `adapters/postgres/tests/test_session_schema.py`,
`adapters/postgres/tests/test_session_workers.py`, `adapters/postgres/README.md`.

**Interfaces:** Built-in stores advertise `context_assembly_v1` after readiness.
PostgreSQL uses explicit conversation storage contract/schema version 2;
`PostgresSessionStore.migrate_context_storage()` is migration-owner-only.
Existing `initialize()` creates the current schema for new databases; `check_ready()`
does not migrate. Add CLI `migrate-conversations` alongside the existing explicit
initialization/check commands; use the existing secret-managed DSN interface.

- [ ] **Step 1:** Add a file-store test which writes and recovers v2, reads a
  retained immutable v1 fixture, and proves a v1-only worker rejects v2 without
  writing it. Add a PostgreSQL test that starts at schema/storage contract 1,
  rejects opted-in creation, explicitly migrates, then accepts both v1/v2 reads.
  Assert old worker readiness fails after the contract migration; do not rely
  solely on later JSON validation to keep an old writer out.
- [ ] **Step 2:** Run `.venv/bin/python -m pytest tests/test_context_file_store.py -q`;
  run PostgreSQL tests with `--require-postgres` on an owned disposable fixture,
  not a shared database. Confirm the new failing assertion before changing DDL.
- [ ] **Step 3:** Add store capabilities and guarded version dispatch. PostgreSQL
  migration holds the existing migration lock, verifies old metadata/constraints,
  updates the contract transactionally and preserves table byte ceilings/roles.
  Version-2 storage still reads v1 JSON; migration must not rewrite historical
  documents. Only the owner role may migrate; runtime cannot acquire DDL rights.
- [ ] **Step 4:** Exercise migration replay, rollback after injected migration
  failure, wrong-role rejection, owner/tenant isolation and stale fencing during
  v2 checkpoint/receipt writes on PostgreSQL 16 and 18. Use existing
  `adapters/postgres/tests/support/local_cluster.py start/test/stop`; retain its
  exact manifest and verify cleanup in all exit paths.
- [ ] **Step 5:** Run file/session/adapter regressions and commit
  `feat: negotiate context storage with explicit postgres migration`.

### Task 7: Harness execution ordering, recovery and interpreter integration

**Repository:** Harness.

**Files:** Modify `src/prosaic_harness/conversation.py`,
`src/prosaic_harness/conversation_workflow.py`,
`src/prosaic_harness/conversation_errors.py` only if closed mapping needs constants.
Create `tests/test_context_conversation.py`, `tests/test_context_recovery.py`.
Extend `tests/test_conversation_workflow.py`, `scripts/conversation_smoke.py`,
`docs/conversations.md`.

**Interfaces:** `_prepare` passes the purpose's explicit policy/registry.
`_reserve` persists prepared selection/report/work via Task 5. `_execute` uses
Task 3 checkpoint and admission hooks. `_saved_preparation` reconstructs only
saved selection and restores its work charges. Public reply optionally exposes
`contextAssembly` as the bounded report, never raw attempt state.

- [ ] **Step 1:** Write a recorder/provider/journal ordering test recording
  `prepare`, `reserve`, `checkpoint`, `readmit`, `marker`, `accounting`, `provider`.
  Assert the first marker follows successful readmission. Inject checkpoint save
  failure/unknown acknowledgement and ensure no marker or provider call occurs.
  Inject marker save unknown acknowledgement: no provider call; recovery reads
  durable authority and does not automatically retry an acknowledged marker.

```python
def test_readmission_precedes_marker(context_host):
    reply = context_host.send('hello')
    assert reply['status'] == 'completed'
    events = context_host.events
    assert events.index('readmit') < events.index('marker')
    assert events.index('marker') < events.index('accounting')
    assert events.index('accounting') < events.index('provider')
```

  Define `context_host` in `tests/support/conversation_host.py` as a fixture
  constructed from the existing real Conversation host, Task 1 synthetic
  registry, existing memory/file SessionStore and recording recorder/provider.
  It must exercise public `Conversation.send`, not replace the reducer/engine.
  Its `send(text)` creates a session once, sends a fresh message ID with the
  actual revision, and returns `ConversationReply.document`; `.events` is a list
  appended at actual fixture boundary callbacks.
- [ ] **Step 2:** Run `.venv/bin/python -m pytest tests/test_context_conversation.py
  tests/test_context_recovery.py -q`.
- [ ] **Step 3:** For opted-in execution, move `mark_dispatch` into `on_admitted`;
  checkpoint saves update the local latest `snapshot` under the same lease using
  revision checks. Legacy execution retains its current path. Recheck lease and
  bindings before hooks; do not count source-history omission again on recovery.
  Map initial Runtime admission errors into existing closed Harness errors:
  context overflow -> `contextLimit`; unsupported -> `capabilityMissing`;
  counter/policy mismatch -> `bindingMismatch`. No exception content is public.
- [ ] **Step 4:** Persist a failed zero-effect receipt after allocated v2
  readmission rejection. Recovery adopts saved receipts without preparation or
  counter/provider calls. Undispatched recovery restores charges, conservatively
  retains interrupted active reservations and executes only the frozen suffix;
  when exhausted it writes a closed zero-effect failure. Dispatched receipt loss
  remains `needsResolution` until existing explicit retry acknowledgement.
  Add crash cases before checkpoint, after reserved checkpoint, after callback
  before reconciliation, after marker and after receipt before adoption.
- [ ] **Step 5:** Exercise interpreter-only/ordinary-only policies on different
  endpoints, current interaction payload mandatory, no interpreter tool grants,
  stale interaction and quoted approval rejection. Verify complete history and
  attribution remain unchanged after omission. Run the full Harness suite and
  required PostgreSQL conversation lifecycle lanes, then commit
  `feat: recover frozen context with durable predispatch readmission`.

### Task 8: Installed-SDK synthetic Lab demonstration and filtered chat reports

**Repository:** Integration Lab.

**Files:** Create `src/prosaic_integration_lab/context_counter.py`,
`lab_runner/context_scenarios.py`, `tests/unit/test_context_counter.py`,
`tests/functional/test_context_assembly.py`,
`tests/acceptance/test_context_assembly.py`.
Modify `src/prosaic_integration_lab/conversation_workflows.py`,
`src/prosaic_integration_lab/conversation_execution.py`,
`src/prosaic_integration_lab/views.py`, `src/prosaic_integration_lab/chat_ui.py`,
`lab_runner/conversation_scenarios.py`, `lab_runner/conversation_demo.py`,
`lab_runner/__main__.py`, `lab_runner/gates.py`,
`scripts/required_acceptance.py`, `scripts/conversation_distribution_smoke.py`,
`docs/conversations.md`. Extend current unit/functional conversation view/UI tests.

**Interfaces:** Lab builds counters only through public Runtime registration
contracts. Host binding definitions add optional `context_policy` and
`interpretation_context_policy` with an allowlisted synthetic counter identity;
request bodies cannot select policies or register code. `public_context_report`
returns an allowlisted bounded view. Existing conversation mode includes the
new acceptance file when the installed manifest/capabilities enable it.

- [ ] **Step 1:** Build Runtime, Harness and Harness PostgreSQL candidate wheels
  with exact commit hashes/checksums; prepare a complete five-wheel candidate
  manifest with unchanged Core/Runtime accounting adapter wheels. Install into a
  fresh Lab candidate environment via existing dependency scripts. No editable
  siblings or private SDK imports. Keep `dependencies/releases.json` unchanged
  until actual publication and verification in Task 9.
  Create the environment with `.venv/bin/python -m venv .venv-context`, install
  Lab/test dependencies with `.venv-context/bin/python -m pip install -e '.[test]'`
  (editable Lab is allowed; editable SDKs are not), then run
  `.venv-context/bin/python scripts/install_dependencies.py --manifest
  .cache/context-assembly/manifest.json` and the corresponding `audit_install.py`.
- [ ] **Step 2:** Add a counter unit test with deterministic fixture request
  grammar, plus real HTTP tests for default full-history rejection and optional
  omission. Seed several conversation turns, send one that fits only after
  omission, and assert stored `/history` still returns all turns. Compare actual
  provider/usage counts: initial rejection causes no accounting intent or call.

```python
def test_report_projection_excludes_untrusted_extra_fields():
    from prosaic_integration_lab.views import public_context_report
    source = {'mode': 'omitOldestTurns', 'inputTokens': 100,
        'inputCapacityTokens': 200, 'retainedTurnCount': 2,
        'omittedTurnCount': 1, 'omittedRange': {'startTurn': 1, 'endTurnExclusive': 2},
        'prompt': 'private-sentinel', 'counterException': 'private-sentinel'}
    view = public_context_report(source)
    assert view['omittedTurnCount'] == 1
    assert 'private-sentinel' not in json.dumps(view)
```

  Define the projection to allow only `mode`, `inputTokens`,
  `inputCapacityTokens`, `retainedTurnCount`, `omittedTurnCount`, `omittedRange`
  and a fixed host label `evidenceKind: synthetic`; independently bound/validate
  each field. Client-supplied context override fields must return a closed 422.
- [ ] **Step 3:** Run candidate `.venv-context/bin/python -m pytest
  tests/unit/test_context_counter.py tests/functional/test_context_assembly.py -q`.
  Implement trusted fixture registration and binding configuration. Synthetic
  counts apply only to the fixture provider/model/framing; refuse enabling them
  in live-local/hosted demos. Preserve existing live demo configuration and tokens.
- [ ] **Step 4:** Render a small context notice in chat: synthetic evidence,
  counted input/capacity and omitted turn range. Use textContent and DOM creation
  rather than inserting provider/report values through HTML. Exact request retry
  must reuse its message ID/body. Report reads go through existing owner checks.
  Add cross-tenant report/history denial and marker/script escaping tests.
- [ ] **Step 5:** Implement a fixed context-quality dataset in
  `lab_runner/context_scenarios.py`: retained recent preference succeeds;
  omitted older preference must visibly abstain/fail the expected-answer check;
  mandatory workflow interaction remains effective; historical prompt injection
  cannot grant tools. Run each on a wide full-history fixture binding and a
  narrow omission binding, record completion/abstention/failure and omitted count,
  and compare outcomes without claiming equal answer quality. Reports contain
  case IDs and outcomes, not private prompts or raw provider logs.
- [ ] **Step 6:** Add acceptance assertions for PostgreSQL recovery across
  replicas, unchanged full history, bounded reservations, interpreter endpoint
  differences, and post-effect context failure preserving journal/accounting.
  Update gate counts and case-name mapping from the actual collected test list;
  do not invent expected counts in advance. Required candidate lanes fail if
  `context_assembly_v1` is missing. Preserve the released legacy conversation lane.
  Extend the currently exact-version `supports_conversation` train check to admit
  the verified new candidate/released train without accepting arbitrary versions;
  require installed capability checks as well as manifest/hash provenance. Add
  tests for missing capability, old released train and a mismatched mixed train.
- [ ] **Step 7:** Run candidate Lab full local suite, build the consumer image,
  then conversation test/demo on PostgreSQL 16/18 using exact candidate manifest.
  Verify owner-specific cleanup and archive receipts. Commit feature files only:
  `feat: demonstrate optional context assembly with installed sdk candidates`.

### Task 9: Distribution qualification and release/adoption handoff

**Repositories:** Runtime, Harness and Integration Lab.

**Files:** Runtime `pyproject.toml`, `docs/conversations.md`,
`scripts/conversation_wheel_smoke.py`, `.github/workflows/test.yml`,
`.github/workflows/publish.yml`; Harness `pyproject.toml`,
`adapters/postgres/pyproject.toml`, `scripts/conversation_smoke.py`,
`.github/workflows/tests.yml`, `.github/workflows/publish.yml`;
Lab `.github/workflows/required-acceptance.yml`, `dependencies/releases.json`,
`README.md`, create `docs/verification/context-assembly-2026-10-14.md`.

**Interfaces:** Existing artifact candidate/promotion machinery remains the
authority for package version, source and hash. Do not fabricate release hashes
or promise a version before candidate qualification. This is a feature release
for Runtime/Harness and a storage-contract release for Harness PostgreSQL;
unchanged Core and Runtime PostgreSQL wheels retain their original bytes.

- [ ] **Step 1:** Run `.venv/bin/python -m pytest` and
  `.venv/bin/python -m build` in each changed SDK repository. Verify clean-wheel
  import/API smoke with `python -I`, both providers, legacy bindings and synthetic
  opt-in. Run Python floor/platform lanes from the existing workflows. Missing
  required infrastructure is a failure; gated live tests remain explicit opt-in.
- [ ] **Step 2:** Run required Harness PostgreSQL 16/18 lanes, schema-1 upgrade,
  old-worker refusal and owner/runtime role checks. Run Lab candidate image tests
  using `python -m lab_runner test --mode conversation --candidate
  --candidate-manifest .cache/context-assembly/manifest.json --postgres-version 16`
  and again on 18, plus `demo` with the same arguments. Candidate install/audit
  scripts must resolve all five exact wheels and reject hash mismatch.
- [ ] **Step 3:** Request one independent whole-branch review using the code-review
  skill after the native implementation. Include spec/plan, actual tests, version
  compatibility and checkpoint/effect evidence. Resolve material findings and
  rerun affected checks; do not call a synthetic counter production-qualified.
- [ ] **Step 4:** Record commands, architectures, source hashes, wheel hashes,
  test counts/skips, resource owners, cleanup receipts and known limitations in
  the Lab ledger. Mark the feature synthetic-qualified and the production
  counter qualification absent. Document explicit migration/writer rollout order.
- [ ] **Step 5:** Prepare concrete reviewable PRs and candidate artifacts. Attach
  every created PR to this task. Use existing session publication authorization
  only where it covers this release; do not merge/publish solely because this
  plan was approved. Once authorized promotion actually succeeds, verify each
  public wheel/hash, archive the previous Lab manifest, update the active manifest
  and rerun installed public adoption. Never update the released manifest to an
  unpublished candidate or overwrite historic evidence.
- [ ] **Step 6:** Commit reviewed qualification/docs/adoption changes in their
  respective repositories. Report the installed/public versions and verification
  limitations accurately; no production tokenizer certification is implied.

## Self-review and handoff

Coverage: policy/counters and ownership (Task 1); deterministic fitting, source
versus request byte ordering, nonmonotonic counters and work bounds (Task 2);
output controls, both provider wires, continuation evidence, time and no automatic
retry (Task 3); distinct ordinary/interpreter binding identity (Task 4); bounded
record overhead and immutable selection/initial report (Task 5); version negotiation
and migration (Task 6); crash recovery, fencing and authority (Task 7); synthetic
quality comparisons, installed API use, owner-filtered report/UI (Task 8); exact
wheels, platforms and release evidence (Task 9). Cost and trajectory projects are
explicitly excluded. Runtime PostgreSQL accounting and Core are unchanged.

This plan is ready for review. Native execution is the preserved method;
implementation begins after the user confirms that this plan captures the scope.
