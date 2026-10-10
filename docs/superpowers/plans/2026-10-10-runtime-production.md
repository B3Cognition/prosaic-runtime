# Runtime Production Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Add reliable optional observation, finite invocation budgets, trusted contextual/journaled tools, and deterministic opt-in qualification without changing disabled execution or historical contracts.

**Architecture:** Runtime retains model transport and tool dispatch; small modules own observer delivery, operation context, invocation counters, effect contracts and conformance evaluation. Harness supplies trusted scope and remaining allowances; the application owns durable business journaling and reconciliation. Existing critical callbacks, accounting and low-level consumer transport defaults remain intact.

**Tech Stack:** Python 3.11+, dataclasses/contextvars/inspect/hashlib/json/sqlite3, existing urllib transports, jsonschema, pytest, synthetic loopback HTTP/SSE.

**Spec:** /Users/michalbachorik/work/prosaics/prosaic/docs/superpowers/specs/2026-10-10-ecosystem-production-design.md

## Global Constraints

- Python imports, CLI commands, repository names, canonical serialized camelCase contracts, and existing manifest/checkpoint/receipt formats remain compatible.
- The application remains responsible for authentication, tenant entitlements, business authorization, HTTP idempotency, deployment configuration, and operator decisions.
- Core distributes prose; Runtime executes models/tools; Harness owns workflow state and orchestration; the lab is a consumer of public installed APIs.
- Keep existing on_event propagating semantics. Validator, authorization, persistence and cancellation failures retain their existing critical behavior.
- Observer records contain no prompts, generated text, credentials, endpoints, filesystem paths or arbitrary exception messages.
- Never promise universal exactly-once effects or hard preemption of arbitrary callbacks. Never reclaim an uncertain claim because a lease or timeout elapsed.
- RunPolicy.max_provider_requests, max_tool_calls and max_reported_tokens are optional nonnegative integers; None preserves legacy behavior and zero means no allowance.
- Harness max_calls continues to mean workflow invocations and is never reinterpreted as provider requests. Preserve checkpoint v2 and receipt v1.
- Disabled new fields remain omitted from legacy descriptors and identity inputs. No Runtime constructor, model, tool or journal callback runs during pure admission/descriptor checks.
- Target distributions are b3-prosaic-runtime 0.8.0 and b3-prosaic-runtime-postgres 0.2.0; Runtime requires b3-prosaic>=0.4,<0.5. Root owns actual version/dependency/publish changes.
- Never install old and renamed distributions into the same environment. SDK metadata has index requirements and no direct Git URLs.
- Synthetic qualification never implies paid calls or deployment-resource mutation. No existing provider, issuer, gateway or shared database is contacted by these tasks.
- Baseline evidence supplied by root: 495 passed and one framework-related skip independently covered. Do not reinstall dependencies or repeat the full baseline before any code changes.
- Work in the existing codex/ecosystem-production branch. Use apply_patch, preserve unrelated changes, and commit only each independently reviewed task's files.

## Review Focus

1. Observer errors, mutation and cancellation at the last instruction must not erase accepted work or cross concurrent scopes: R1 tests observer_failure_preserves_result, terminal_after_cancel and concurrent_observer_scopes.
2. No-tools OpenAI execution and terminal native responses must enforce unknown/over-cap usage just like tool loops: R2 tests terminal_usage_matrix and acquisition_shares_budget.
3. Equal operation keys across namespaces, stale tokens, post-effect invalid results and lost commit acknowledgement must never double-dispatch or overwrite outcomes: R3 tests namespace_isolation, stale_commit, unusable_outcome and lost_ack_replay.
4. Offline qualification must not execute doctor discovery/probes, leak provider text or certify another profile: R4 tests no_live_has_no_effects, canonical_report and profile_bound_evidence.
5. Renamed package installation, minimum dependency floors and journal/observer failures must preserve accounting and legacy descriptor bytes: R5 tests wheel_metadata, legacy_descriptor_golden and accounting_composition.

## File and API ownership

| Unit | Exact files | Responsibility |
| --- | --- | --- |
| Scope/observation | src/prosaic_runtime/operation_context.py; src/prosaic_runtime/telemetry.py | Immutable trusted scope and safe optional delivery shared with Harness. |
| Invocation allowance | src/prosaic_runtime/budgets.py; policy.py | Counters and strict reported-usage decisions, independent of persistence. |
| Tool effects | src/prosaic_runtime/tool_effects.py; tools.py; tool_registry.py | Public journal protocol, pure descriptors, contextual dispatch and safe uncertainty. |
| Qualification | src/prosaic_runtime/conformance.py; diagnostics.py; cli.py | Pure versioned evaluation and explicit bounded runner. |
| Adapter acceptance | adapters/postgres/tests/test_runtime_capture.py; tests/test_package_compatibility.py | Existing accounting composition and installed renamed package checks. |

R1 precedes R2 and R3. R2 precedes the bounded runner in R4. R3 precedes the lab journal and Harness effect binding integration. R5 runs after root stages candidate distribution metadata and wheels; it never publishes them.

---

### Task 1: R1 — Optional observer and independent InvocationScope

**Files**

- Create src/prosaic_runtime/operation_context.py and src/prosaic_runtime/telemetry.py.
- Modify src/prosaic_runtime/events.py, runtime.py and __init__.py.
- Create tests/test_telemetry.py; extend tests/test_artifact_admission.py.
- Update README.md and create docs/telemetry.md.

**Interfaces**

- Export frozen InvocationScope(invocation_id: str, run_id: str | None = None, step_id: str | None = None, operation_namespace: str | None = None).
- Add run(..., observer=None, operation_context: InvocationScope | None = None), without changing context/accounting enablement.
- Export ObserverEmitter(observer, *, source: str, scope: InvocationScope, labels=None), with emit(event: str, **fields) -> None.
- Add capability observer_v1. Do not add accounting metadata merely because operation_context is supplied.
- Observer records are owned dictionaries, not borrowed critical events. Runtime event names: invocation_started, invocation_completed, provider_request_started, provider_request_completed, tool_started, tool_completed.
- Harness event names accepted by the shared helper: run_started, transition_committed, waiting_committed, recovery_committed, blocked_committed, run_completed.

- [ ] **Step 1: Write real regression tests before implementation.**

~~~python
import json
import pytest
from prosaic_runtime import InvocationScope
from test_runtime import artifact, completion, runtime, server

def test_observer_failure_preserves_result(server):
    url, requests, responses = server
    from prosaic_runtime import EndpointConfig
    responses.append(completion("done"))
    records = []
    def observer(record):
        records.append(dict(record))
        record.clear()
        raise RuntimeError("PRIVATE_OBSERVER_MESSAGE")
    result = runtime(EndpointConfig(url, "test", features={"streaming": False})).run(
        artifact(), observer=observer,
        operation_context=InvocationScope("attempt-1", run_id="run-1"))
    assert result.exit_code == 0 and result.stdout == "done"
    assert len(requests) == 1
    assert [r["event"] for r in records].count("invocation_completed") == 1
    assert "PRIVATE_OBSERVER_MESSAGE" not in json.dumps(records)
    assert "accounting_v1" not in result.metadata

def test_terminal_after_cancel():
    records = []
    result = runtime().run(artifact(), cancelled=lambda: True,
                           observer=records.append)
    assert result.exit_code == 130
    assert records[-1]["event"] == "invocation_completed"
    assert records[-1]["outcome"] == "cancelled"
~~~

- [ ] **Step 2: Verify RED.**

Run .venv/bin/python -m pytest tests/test_telemetry.py -q.
Expected initial ImportError for InvocationScope or TypeError for observer; after the interface exists, the result-preservation and terminal assertions must fail until delivery is wired.

- [ ] **Step 3: Implement immutable scope and bounded delivery.**

~~~python
@dataclass(frozen=True)
class InvocationScope:
    invocation_id: str
    run_id: str | None = None
    step_id: str | None = None
    operation_namespace: str | None = None

class ObserverEmitter:
    def emit(self, event, **fields):
        record = self._record(event, fields)
        if record is None or self.observer is None:
            return
        try:
            self.observer(dict(record))
        except Exception:
            self.failures += 1
~~~

Scope values are plain strings, nonempty when supplied, at most 128 UTF-8 bytes, with no Unicode control/format/surrogate characters. Invalid scope or labels raises ValueError before dispatch. Runtime generates one UUID invocation ID when scope is omitted; it does not resolve billing defaults.

Define closed event/source/outcome/reason sets. Records contain version=1, monotonically increasing sequence, source and opaque scope references. Runtime invocation outcomes are completed, cancelled, timed_out, admission_failure, critical_hook_error, budget_failure and execution_failure. Harness outcomes are the actual committed checkpoint states running, waiting, blocked, completed or rejected; allow its calls as a nonnegative integer and revision as a bounded opaque string or nonnegative integer. Allow only bounded registered tool names, digests, counts, finite nonnegative durations and status where applicable. Omit unknown supplied fields and redact unregistered model tool names to unknown. Cap a record at 4096 UTF-8 bytes; accept at most 16 explicit public labels with key/value byte limits 64/128. Preserve event/outcome/revision/calls when bounding a valid committed record, dropping optional labels first. Invalid dynamic fields are dropped rather than raising after an effect.

Use per-invocation ContextVar state, reset in finally. Keep legacy emit's cancellation and critical sink behavior unchanged. Emit optional provider observations around open_http, including failure outcomes, with no captured request/headers. Emit the single Runtime terminal observation from the public run wrapper for Result outcomes and escaped admission/critical-hook failures. Its delivery does not call check_cancelled. Ordinary observer exceptions cannot alter Result or legacy exception propagation; BaseException process-control exceptions remain visible.

- [ ] **Step 4: Add fault/concurrency and purity tests.**

~~~python
def test_legacy_critical_callback_still_propagates(server):
    from prosaic_runtime import EndpointConfig
    url, requests, responses = server
    responses.append(completion("done"))
    def critical(event):
        if event["event"] == "completed":
            raise RuntimeError("critical")
    records = []
    with pytest.raises(RuntimeError, match="critical"):
        runtime(EndpointConfig(url, "test", features={"streaming": False})).run(
            artifact(), on_event=critical, observer=records.append)
    assert len(requests) == 1
    assert records[-1]["outcome"] == "critical_hook_error"
~~~

Also test admission failure, timeout, observer KeyboardInterrupt propagation, independently sequenced concurrent invocations using ThreadPoolExecutor, oversized hostile text/path/canary fields, bounded labels, and a pure validate_execution_artifact call that constructs neither emitter nor Runtime and executes no callback. Check that no observer record contains text_delta content or legacy progress previews.

Add a shared-helper test using source="harness", outcome="blocked", revision="opaque-revision", calls=1 and a maximum-size label set; the resulting bounded record must retain the actual committed state, revision and call count.

- [ ] **Step 5: Verify GREEN and document scope.**

Run .venv/bin/python -m pytest tests/test_telemetry.py tests/test_artifact_admission.py tests/test_runtime.py tests/test_custom_tool_transport.py tests/test_accounting.py -q.
Run git diff --check. Document critical versus optional callbacks, scalar report fields, synchronous callback limits and the absence of durable delivery.

- [ ] **Step 6: Independent review, then commit only R1 files.**

Run git add src/prosaic_runtime/operation_context.py src/prosaic_runtime/telemetry.py src/prosaic_runtime/events.py src/prosaic_runtime/runtime.py src/prosaic_runtime/__init__.py tests/test_telemetry.py tests/test_artifact_admission.py README.md docs/telemetry.md.
Run git commit -m "feat: add isolated optional runtime observation".

### Task 2: R2 — Finite invocation allowances

**Files**

- Create src/prosaic_runtime/budgets.py and tests/test_invocation_budgets.py.
- Modify policy.py, runtime.py and tool_registry.py.
- Modify execution.py/openai_compatible.py only for default-no-op hooks required by the bounded public Runtime; preserve low-level consumer defaults.
- Update README.md and docs/telemetry.md.

**Interfaces**

- Add RunPolicy.max_provider_requests, max_tool_calls and max_reported_tokens: int | None = None.
- Add internal InvocationBudget(policy), before_provider(), before_tool(), record_usage(usage: int | None), validate_terminal_usage().
- Add internal InvocationBudgetExceeded(reason) with closed reasons provider_request_limit, tool_call_limit, token_limit and usage_unknown.
- Add capability invocation_budgets_v1.
- Return these reasons through Result.metadata.failure_reason, preserve known/partial usage and existing Result fields. New count metadata is optional and emitted only when a new cap is enabled.

- [ ] **Step 1: Write batch and terminal boundary tests.**

~~~python
from dataclasses import replace
import json
import pytest
from prosaic_runtime import ProsaicRuntime, RunPolicy
from test_runtime import artifact, completion, server
from test_custom_tool_transport import setup, call

def test_multicall_batch_counts_attempts(server):
    url, requests, responses = server
    seen = []
    config, tool = setup(url, seen)
    responses.append(completion("", [call(), call(), call()]))
    result = ProsaicRuntime(config, custom_tools={"lookup_catalog": tool}).run(
        artifact(["lookup_catalog"]),
        policy=RunPolicy(allowed_tools=frozenset({"lookup_catalog"}),
                         max_tool_calls=1))
    assert len(seen) == 1 and len(requests) == 1
    assert result.metadata["failure_reason"] == "tool_call_limit"
    assert result.token_usage == 7

@pytest.mark.parametrize("usage,cap,reason", [
    (None, 7, "usage_unknown"),
    ({"total_tokens": 8}, 7, "token_limit"),
    ({"total_tokens": 7}, 7, None),
])
def test_terminal_usage_no_tools(server, usage, cap, reason):
    from prosaic_runtime import EndpointConfig
    from test_runtime import runtime
    url, requests, responses = server
    kind, body = completion("done")
    value = json.loads(body)
    value.pop("usage", None)
    if usage is not None:
        value["usage"] = usage
    responses.append((kind, json.dumps(value).encode()))
    result = runtime(EndpointConfig(url, "test", features={"streaming": False})).run(
        artifact(), policy=RunPolicy(max_reported_tokens=cap))
    assert len(requests) == 1
    assert result.exit_code == (0 if reason is None else 1)
    assert result.metadata.get("failure_reason") == reason
~~~

- [ ] **Step 2: Verify RED.**

Run .venv/bin/python -m pytest tests/test_invocation_budgets.py -q.
Expected TypeError for new RunPolicy fields, followed by actual dispatch/usage assertion failures until enforced.

- [ ] **Step 3: Implement single-boundary counters.**

~~~python
def before_provider(self):
    self._check_reported_allowance()
    self._reserve("provider_requests", self.policy.max_provider_requests,
                  "provider_request_limit")

def before_tool(self):
    self._check_reported_allowance()
    self._reserve("tool_calls", self.policy.max_tool_calls, "tool_call_limit")

def record_usage(self, usage):
    if usage is None:
        self.usage_complete = False
    else:
        self.reported_tokens += usage
    if self.policy.max_reported_tokens is not None:
        if not self.usage_complete:
            raise InvocationBudgetExceeded("usage_unknown")
        if self.reported_tokens > self.policy.max_reported_tokens:
            raise InvocationBudgetExceeded("token_limit")
~~~

Validate integer fields strictly: reject bool, float, negative and nonfinite values; zero is valid. Reserve a provider attempt before accounting preparation/HTTP opening, and a tool attempt once at BoundedToolRegistry.execute_message before name/argument rejection. Repeated deadline checks must not consume counts. Denied calls and journal replay consume allowance.

For tool loops, account usage immediately after each returned ProviderTurn, before tool dispatch. Preserve Capture observation persistence and backend completed-turn usage before raising a budget failure. OpenAI no-tools execution does not use _post_chat_turn: normalize/validate its terminal Result usage too, exactly once. Native Anthropic uses the shared tool loop even without tools. Missing/invalid/partial totals become unknown; never promote reported partial totals into a complete count.

Before additional dispatch, total equal to the token cap is exhausted; a final complete response at equality succeeds. Overshoot and unknown terminal usage fail even without a follow-up. A configured zero token/request allowance prevents network. A zero tool allowance permits a text-only request but prevents tool dispatch.

Do not mutate EndpointConfig max_tokens into an estimated input-plus-output ceiling. No provider retries, money cap or tokenizer dependency is added.

- [ ] **Step 4: Add the provider/streaming matrix and retained evidence.**

~~~python
def test_acquisition_shares_provider_budget(server, tmp_path):
    from prosaic_runtime import EndpointConfig
    from test_runtime import runtime
    from test_acquisition import read
    url, requests, responses = server
    (tmp_path / "input").write_text("evidence")
    responses.append(completion("", read()))
    result = runtime(EndpointConfig(url, "test", features={"streaming": False})).run(
        artifact("read"), acquisition=artifact("read"), cwd=tmp_path,
        policy=RunPolicy(allowed_tools=frozenset({"read_file"}),
                         read_roots=("input",), initial_tool="read_file",
                         max_provider_requests=1))
    assert len(requests) == 1 and result.token_usage == 7
    assert result.metadata["failure_reason"] == "provider_request_limit"
~~~

Repeat terminal usage tests with native_server/response/message and with SSE fixtures from test_anthropic_stream and test_acquisition.reply. Test invalid quantities, multi-round missing usage, zero allowances, denied/malformed tool attempts, initial-tool mismatch, failed HTTP requests, post-tool cancellation, recording failure and native usage details. Run a disabled-policy golden test comparing outputs/metadata/descriptor data to released behavior. Harness remaining max_calls is not a test input to provider-request allowance.

- [ ] **Step 5: Verify GREEN, including existing boundary tests.**

Run .venv/bin/python -m pytest tests/test_invocation_budgets.py tests/test_usage_integrity.py tests/test_accounting.py tests/test_anthropic_accounting.py tests/test_acquisition.py tests/test_custom_tool_transport.py -q.
Run git diff --check. Document separate units, terminal unknown-usage failure and possible one-request overshoot.

- [ ] **Step 6: Independent review, then commit only R2 files.**

Stage budgets.py, policy.py, runtime.py, tool_registry.py, explicitly changed default-no-op transport seams, tests/test_invocation_budgets.py and documentation.
Run git commit -m "feat: bound provider and tool dispatch within invocations".

### Task 3: R3 — Contextual tools and host journal protocol

**Files**

- Extend operation_context.py; create tool_effects.py.
- Modify tools.py, tool_registry.py, runtime.py, structured_tools.py and __init__.py.
- Create tests/test_tool_effects.py, tests/test_tool_effect_workers.py and tests/support/tool_journal.py.
- Update docs/structured-tools.md; create docs/tool-effects.md.

**Interfaces**

- Add CustomTool(..., with_context=False, operation_key=None). One flag explicitly opts both handler and supplied authorizer into (arguments, ToolExecutionContext); ordinary tools retain one argument.
- Export frozen ToolExecutionContext(scope: InvocationScope, tool_name: str, tool_version: str, call_index: int, arguments_sha256: str, deadline: float, cancelled: Callable, operation_key: str | None = None).
- Export frozen ToolClaim(state: str, signature: str, claim_token: str | None = None, outcome: dict | None = None).
- Export ToolJournal Protocol with plain identity: str and contract_version="tool-journal-v1"; claim(operation_namespace: str, operation_key: str, signature: str) -> ToolClaim; commit(operation_namespace: str, operation_key: str, claim_token: str, bounded_outcome: dict) -> None.
- Export tool_journal_descriptor(journal) -> dict. None produces {}; otherwise inspect declared plain instance/class scalars with inspect.getattr_static, reject properties/dynamic descriptors, and statically check callable claim/commit without invoking methods.
- Add run(..., tool_journal=None); StructuredToolLoop(..., operation_context=None, tool_journal=None, observer=None) uses the same contextual dispatch.
- Add capabilities tool_context_v1 and tool_journal_v1. Expose bounded fixed failure codes tool_effect_uncertain, tool_journal_failed and tool_identity_conflict; never forward host exception messages.

- [ ] **Step 1: Write contextual and journal replay tests.**

The test-only SqliteJournal in tests/support/tool_journal.py uses stdlib sqlite3, never a production recorder. Its constructor initializes a temporary table keyed by (namespace, operation_key), with signature, fencing token and optional canonical outcome. claim uses BEGIN IMMEDIATE; an absent row inserts a random token and returns new, an existing outcome returns replay, and an uncommitted row returns uncertain. Signature mismatch returns the stored signature for Runtime rejection. commit updates only the matching token and empty outcome; rowcount != 1 raises. No timeout, expiry, cleanup or automatic reconciliation exists.

~~~python
import json
import pytest
from prosaic_runtime import (CustomTool, InvocationScope, ProsaicRuntime,
                            RunPolicy, ToolClaim)
from test_runtime import artifact, completion, server
from test_custom_tool_transport import setup, call
from support.tool_journal import SqliteJournal

def test_journal_replay_does_not_execute_twice(server, tmp_path):
    url, requests, responses = server
    seen = []
    config, original = setup(url, seen)
    def handler(arguments, context):
        seen.append(context.operation_key)
        return {"found": True}
    tool = CustomTool("lookup_catalog", "Lookup", original.parameters, handler, "v2",
        with_context=True, operation_key=lambda args, ctx: "selection-1")
    journal = SqliteJournal(tmp_path / "journal.sqlite")
    runtime = ProsaicRuntime(config, custom_tools={"lookup_catalog": tool})
    policy = RunPolicy(allowed_tools=frozenset({"lookup_catalog"}))
    for attempt in ("first", "second"):
        responses.extend([completion("", [call()]), completion("done")])
        result = runtime.run(artifact(["lookup_catalog"]), policy=policy,
            tool_journal=journal,
            operation_context=InvocationScope(attempt, run_id="run",
                                              operation_namespace="tenant-a"))
        assert result.exit_code == 0
    assert seen == ["selection-1"] and len(requests) == 4
    assert journal.claim("tenant-a", "selection-1", journal.signature(
        "tenant-a", "selection-1")).state == "replay"
~~~

Define SqliteJournal.signature(namespace,key) as a test helper selecting the stored hash. Its identity is a plain bounded constant assigned by the test host; physical paths never appear in descriptors.

- [ ] **Step 2: Verify RED.**

Run .venv/bin/python -m pytest tests/test_tool_effects.py -q.
Expected missing exported types/with_context; after interfaces are added, expect duplicate handler execution until journal dispatch is wired.

- [ ] **Step 3: Implement pure registration and bounded dispatch.**

~~~python
def tool_journal_descriptor(journal):
    if journal is None:
        return {}
    try:
        version = inspect.getattr_static(journal, "contract_version")
        identity = inspect.getattr_static(journal, "identity")
        methods = [inspect.getattr_static(journal, name)
                   for name in ("claim", "commit")]
    except AttributeError:
        raise ValueError("invalid tool journal declarations") from None
    if type(version) is not str or version != "tool-journal-v1":
        raise ValueError("invalid tool journal contract")
    if type(identity) is not str or not valid_identifier(identity):
        raise ValueError("invalid tool journal identity")
    for method in methods:
        if not callable(method):
            raise ValueError("invalid tool journal methods")
    return {"contract_version": version, "identity": identity}
~~~

Implement valid_identifier in tool_effects.py using the scope bounds from R1. Key and claim-token bounds are 256 UTF-8 bytes with control/format/surrogate rejection; journal identity/namespace use the 128-byte identifier bound. InvocationScope namespace is mandatory when any registered operation_key resolver is requested. Require with_context=True for a resolver. A journaled tool missing required scope/journal fails before HTTP; construction, admission and descriptor access never call a resolver, authorizer, handler or journal.

CustomTool.version versions handler, authorizer, captured fixed data and resolver semantics together. Add with_context=true and journaled=true descriptor keys only when enabled. Existing descriptor dictionaries remain exact when disabled. Journal metadata is sealed by Harness together with operation_namespace; no object IDs, DSNs, paths or callback source are serialized.

The argument digest/signature use compact sorted UTF-8 finite JSON with ensure_ascii=False. Signature SHA-256 covers tool name, CustomTool.version and validated arguments. Pass independent argument copies to authorizer, resolver and handler; mutation cannot change checked arguments or signature. The authorizer sees context with operation_key=None; resolve the key only after authorization, then give the handler a replaced context containing it.

~~~python
# After validation/grant/authorization/budget checks:
claim = journal.claim(namespace, key, signature)
validate_claim(claim, signature, tool.max_result_bytes)
if claim.state == "uncertain":
    raise ToolEffectFailure("tool_effect_uncertain")
if claim.state == "replay":
    return snapshot_outcome(claim.outcome)
value = invoke_handler(tool, arguments, context)
outcome = require_valid_bounded_outcome(value, tool.max_result_bytes)
journal.commit(namespace, key, claim.claim_token, outcome)
check_boundary()
return outcome
~~~

Add ToolEffectFailure with closed reason codes. The public Runtime converts it into a failed Result retaining completed-turn usage; StructuredToolLoop raises StructuredToolError with a fixed reason. Neither path feeds it into model correction. Invalid/oversized post-effect results leave the claim uncertain rather than committing an ordinary tool-error envelope. Lost commit acknowledgement fails the current invocation; a subsequent matching claim may replay a durable committed outcome, but never redispatch an uncertain effect.

validate_claim requires an exact ToolClaim, matching lowercase 64-hex signature, valid closed state, fencing token only for new, and a finite closed {"status":"ok","result":...} outcome only for replay. Deep-copy replay data and recheck current result bounds. Stale-token commit rejection, host resolution and domain effect atomicity remain host duties. Do not implement a Runtime reconciliation API or TTL reclaim.

- [ ] **Step 4: Add uncertainty, namespace, fencing and worker tests.**

~~~python
def test_unusable_outcome_stays_uncertain(server, tmp_path):
    url, requests, responses = server
    seen = []
    config, original = setup(url, seen)
    tool = CustomTool("lookup_catalog", "Lookup", original.parameters,
        lambda args, ctx: seen.append(ctx.operation_key) or {"bad": float("nan")},
        "v2", with_context=True, operation_key=lambda args, ctx: "action")
    journal = SqliteJournal(tmp_path / "journal.sqlite")
    responses.append(completion("", [call()]))
    result = ProsaicRuntime(config, custom_tools={"lookup_catalog": tool}).run(
        artifact(["lookup_catalog"]), tool_journal=journal,
        operation_context=InvocationScope("attempt", operation_namespace="tenant"),
        policy=RunPolicy(allowed_tools=frozenset({"lookup_catalog"})))
    assert result.metadata["failure_reason"] == "tool_effect_uncertain"
    assert len(requests) == 1 and seen == ["action"]
    assert journal.claim("tenant", "action", journal.signature(
        "tenant", "action")).state == "uncertain"
~~~

Add tests for equal key in two namespaces; changed arguments/version signature; concurrent claim through separate sqlite connections; invalid claim shapes; stale fencing token cannot overwrite committed data; authorizer and resolver mutation; properties on journal metadata are rejected without access; cancellation immediately after handler still commits before returning cancelled; commit writes then raises (lost_ack_replay); handler raise/cancel remains uncertain.

tests/test_tool_effect_workers.py runs two fresh Python -I subprocesses with explicit Runtime source/test support paths, host argv storage and scope, and the same trusted registration. The first invokes a harmless append/count effect and commits; the second replays without another append. A separate worker uses os._exit after the append but before commit; the next worker must return uncertainty and preserve the single effect. Use parent timeout=20 and inspect return codes/host files; no sleeps, Node or live provider. The production PostgreSQL journal and authoritative reconciliation tests belong to the lab plan.

- [ ] **Step 5: Verify GREEN and old signatures.**

Run .venv/bin/python -m pytest tests/test_tool_effects.py tests/test_tool_effect_workers.py tests/test_custom_tools.py tests/test_custom_tool_transport.py tests/test_structured_tools.py tests/test_artifact_admission.py -q.
Run git diff --check. Document domain-key ownership, journal identity/version trust, no preemption, no automatic reconciliation and the distinction between model request and business operation replay.

- [ ] **Step 6: Independent review, then commit only R3 files.**

Stage operation_context.py, tool_effects.py, modified dispatch/export modules, named effect tests/support helper and docs/tool-effects.md/docs/structured-tools.md.
Run git commit -m "feat: support trusted contextual and journaled tool execution".

### Task 4: R4 — Deterministic conformance and bounded scenario reports

**Files**

- Create src/prosaic_runtime/conformance.py and tests/test_conformance.py.
- Modify diagnostics.py, cli.py and __init__.py.
- Extend tests/test_features.py and tests/test_anthropic_package.py.
- Create docs/conformance.md; update README.md.

**Interfaces**

- Export evaluate_conformance(suite: str, profile_fingerprint: str, observations: dict) -> dict.
- Add diagnostics.conformance(config, profile, live=False, policy=None, observer=None, *, evidence_origin="live") -> dict. The explicit origin is a trusted host declaration; local fixture acceptance supplies "fixture".
- Add conformance_v1; CLI prosaic-runtime conformance --config PATH --profile NAME requires --live for network execution, otherwise returns explicit not_run/unknown evidence.
- Initial suite is text-tools-v1 with ordered checks text_complete, stream_terminal, required_tool, arguments_validated, strict_json, usage_complete and cancellation_boundary.
- A scenario observation is a finite, closed dictionary of assertion state/reason, suiteId, profileFingerprint, origin and digests/counts; it contains no raw provider output. suiteId/profileFingerprint bind evidence to the tested suite/configuration; origin is fixture or live. The evaluator accepts only known suite/case names and states.

- [ ] **Step 1: Write pure evaluator and zero-effect tests.**

~~~python
import json
from prosaic_runtime import evaluate_conformance
from prosaic_runtime.diagnostics import conformance
from test_artifact_admission import config

def test_canonical_report():
    observations = {"text_complete": {"state": "passed", "reason": "complete",
        "suiteId": "text-tools-v1", "profileFingerprint": "a" * 64,
        "origin": "fixture"}}
    a = evaluate_conformance("text-tools-v1", "a" * 64, observations)
    b = evaluate_conformance("text-tools-v1", "a" * 64,
                             dict(reversed(list(observations.items()))))
    encode = lambda value: json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False)
    assert encode(a) == encode(b)
    assert a["version"] == 1 and a["cases"][0]["id"] == "text_complete"
    assert a["cases"][0]["origin"] == "fixture"
    assert a["qualification"] == "not_qualified"
    assert {case["state"] for case in a["cases"][1:]} == {"not_run"}

def test_no_live_has_no_effects(monkeypatch):
    import urllib.request
    from prosaic_runtime import ProsaicRuntime
    import subprocess
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected effect")
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", forbidden)
    monkeypatch.setattr(ProsaicRuntime, "__init__", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    report = conformance(config(), "openai", live=False)
    assert report["qualification"] == "not_qualified"
    assert all(case["state"] == "not_run" for case in report["cases"])
~~~

- [ ] **Step 2: Verify RED.**

Run .venv/bin/python -m pytest tests/test_conformance.py -q.
Expected missing public evaluator/runner, then report/delivery assertion failures.

- [ ] **Step 3: Implement closed evaluation before the runner.**

~~~python
def evaluate_conformance(suite, profile_fingerprint, observations):
    try:
        definition = SUITES[suite]
    except (KeyError, TypeError):
        raise ValueError("unknown conformance suite") from None
    owned = validate_observations(observations, definition, suite,
                                  profile_fingerprint)
    cases = [case_record(case, owned.get(case)) for case in definition]
    return {"version": 1, "suiteId": suite, "runtimeVersion": runtime_version(),
            "profileFingerprint": profile_fingerprint, "cases": cases,
            "qualification": qualification(cases)}
~~~

Define validate_observations, case_record, qualification and runtime_version in conformance.py. Allowed case states are passed, failed, unsupported, unknown, not_run; fixed reasons include complete, incomplete, not_requested, not_executed, unsupported_feature, invalid_arguments, invalid_output, usage_unknown and cancelled_before_followup. Cases missing evidence use not_run/not_executed and origin=unexecuted. qualification is qualified only when every required executed case passes with origin=live; all-fixture required passes yield fixture_passed, and every other combination yields not_qualified. Unknown/failed/not_run cannot count as success. Unsupported configured-off checks are reported separately from failures. Reject foreign case names, mismatched suite/profile bindings, malformed fingerprint, invalid origin, nonfinite values, raw-text/URL/header fields and excessive bytes/depth with ValueError; maximum observations/report size is 65536 UTF-8 bytes and depth 16.

For text-tools-v1, all seven checks are required. A profile explicitly disabling streaming or JSON mode records the corresponding check as unsupported/not_requested and does not qualify for that complete suite; it still receives its other individual measured results. Store these requirements in the versioned suite definition; do not let imported observations declare themselves optional. Smaller suites are a future versioned addition, not an inference from an opaque profile digest.

The canonical report has no timestamps, timings, response IDs or raw model names/configuration. Runtime version comes from the public source value prosaic_runtime.__version__ staged by root with candidate packaging. The installed-wheel gate requires that value to equal b3-prosaic-runtime metadata; never label changed source using stale legacy editable-distribution metadata. A host-only diagnostics sidecar may carry timings, separate from canonical comparison data.

The profile fingerprint uses versioned canonical provider/model/features/temperature/max_tokens/response-byte settings and base URL identity; never actual credentials or their values. Expose only its digest. Evidence records must carry the matching profile fingerprint and suite identity when imported/replayed; reject another profile's evidence. Fixture and live evidence have separate origin values, and fixture origin never certifies a live profile. Origin declarations and imported assertion evidence are host responsibilities, not cryptographically verified provider attestations.

live=False returns before constructor, discovery or probe code. live=True constructs only synthetic inspected ProsaicArtifact values and a synthetic read-only CustomTool in private temporary data. Reuse ProsaicRuntime, R1 observer and R2 allowances; do not call doctor or require Prosaic executable. Require finite caller policy or apply an explicit default shared suite allowance: timeout_s=60, max_provider_requests=12, max_tool_calls=4, max_reported_tokens=32768, max_input_bytes=65536, max_tool_rounds=2. Keep a single absolute suite deadline and subtract prior calls/usage before each scenario; unknown usage halts remaining live dispatch. Each scenario output/result and observed data is bounded at 65536 bytes.

Local tool/input/schema assertions use strict finite JSON and host-owned deterministic predicates. No model judges or client Python code; no new general schema engine. The cancellation_boundary check runs a synthetic read-only callback and cancels after it, verifying no follow-up request. Retain existing smoke/doctor report behavior when the new command is unused.

- [ ] **Step 4: Test genuine observed capability and CLI gates.**

Use server/native_server plus existing SSE builders for complete text, streamed terminal events, enforced first native tool, invalid tool arguments with zero handler calls, strict JSON fixture schema, missing usage, truncated streams, cancellation and shared-suite budget exhaustion. Check that prose claiming a tool was used fails required_tool; json_mode configuration alone does not pass strict_json; terminal markers without observed streaming do not pass stream_terminal.

~~~python
def test_profile_bound_evidence():
    import pytest
    evidence = {"text_complete": {"state": "passed", "reason": "complete",
                                  "suiteId": "text-tools-v1",
                                  "profileFingerprint": "b" * 64,
                                  "origin": "fixture"}}
    with pytest.raises(ValueError):
        evaluate_conformance("text-tools-v1", "a" * 64, evidence)
~~~

Test CLI without --live using a fake unreachable URL and a subprocess parent timeout, assert zero fixture requests and not_qualified. Test both providers with a complete synthetic suite and closed report reasons; API live=True is used only with the localhost fixture and evidence_origin="fixture" during implementation. All-fixture passing evidence must produce fixture_passed rather than qualified, and mixing fixture/live evidence cannot qualify.

- [ ] **Step 5: Verify GREEN and diagnostics compatibility.**

Run .venv/bin/python -m pytest tests/test_conformance.py tests/test_features.py tests/test_anthropic_package.py tests/test_anthropic_stream.py -q.
Run git diff --check. Document canonical serialization, origin/profile binding, explicit network opt-in, measured versus requested features and the absence of live-provider certification.

- [ ] **Step 6: Independent review, then commit only R4 files.**

Stage conformance.py, diagnostics.py, cli.py, exports, named tests, README.md and docs/conformance.md.
Run git commit -m "feat: report deterministic opt-in provider conformance".

### Task 5: R5 — Runtime adapter and installed-package compatibility acceptance

**Files**

- Create tests/test_package_compatibility.py and scripts/production_wheel_smoke.py.
- Extend tests/test_accounting.py, adapters/postgres/tests/test_runtime_capture.py and tests/test_custom_tools.py.
- Create docs/production-acceptance.md.
- Root exclusively owns pyproject.toml, adapter metadata, publish workflows, version numbers, tags and index/GitHub uploads.

**Interfaces**

- Consume R1–R4 exports and root's staged b3-prosaic-runtime 0.8.0 / recorder 0.2.0 candidate wheels.
- Smoke entry: python -I scripts/production_wheel_smoke.py, from a clean environment containing only the new distribution family.
- No migration of existing accounting table/intent/observation formats is introduced. The journal is independent of the PostgreSQL usage recorder.

- [ ] **Step 1: Pin legacy descriptor and accounting invariants in tests.**

~~~python
def test_legacy_descriptor_golden():
    from prosaic_runtime import CustomTool
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    value = CustomTool("lookup", "Lookup", schema, lambda args: {}, "v1")
    assert value.descriptor == {
        "name": "lookup", "description": "Lookup", "parameters": schema,
        "version": "v1", "max_argument_bytes": 16384,
        "max_result_bytes": 65536, "authorization_required": False}

def test_operation_scope_does_not_enable_accounting(server):
    from prosaic_runtime import EndpointConfig, InvocationScope
    from test_runtime import runtime, artifact, completion
    url, requests, responses = server
    responses.append(completion("done"))
    result = runtime(EndpointConfig(url, "test", features={"streaming": False})).run(
        artifact(), operation_context=InvocationScope("attempt", run_id="run"))
    assert "accounting_v1" not in result.metadata and len(requests) == 1
~~~

Extend test_network_to_durable_scoped_estimate to attach a failing observer and a supplied InvocationScope while retaining real ledger counts/decimal result/privacy. Add a token-cap failure after a paid fixture turn and verify that usage observation remains durable. Add a journal failure after a paid fixture response and verify one recorded provider request, no effect dispatch and no auto retry. These database tests require an explicitly owned local DSN and run on existing PostgreSQL 16/18 lanes; skipped database tests are not release proof.

- [ ] **Step 2: Verify RED for metadata and public exports when packaging is staged.**

~~~python
def test_wheel_metadata():
    import os
    from importlib.metadata import distribution, PackageNotFoundError
    from packaging.requirements import Requirement
    import pytest
    if os.environ.get("PROSAIC_RUNTIME_WHEEL_ACCEPTANCE") != "1":
        pytest.skip("requires the separately mandatory clean installed-wheel gate")
    import prosaic_runtime
    core = distribution("b3-prosaic-runtime")
    assert core.version == "0.8.0"
    assert prosaic_runtime.__version__ == core.version
    requirements = [Requirement(raw) for raw in core.requires or []]
    assert all(item.url is None for item in requirements)
    requirement = next(item for item in requirements if item.name == "b3-prosaic")
    assert "0.4.0" in requirement.specifier and "0.5.0" not in requirement.specifier
    for legacy in ("prosaic", "prosaic-runtime", "prosaic-runtime-postgres"):
        with pytest.raises(PackageNotFoundError):
            distribution(legacy)
~~~

Run this installed-only metadata test with PROSAIC_RUNTIME_WHEEL_ACCEPTANCE=1 in the clean candidate wheel environment, not the existing editable venv. The source-suite skip is explicit and cannot satisfy the separately mandatory installed gate. Until root stages renamed wheels the installed run is expected to fail; do not resolve failure by installing unrelated prosaic or changing metadata yourself.

- [ ] **Step 3: Implement the installed-wheel smoke using public APIs.**

The script asserts sys.flags.isolated, all relevant module origins under site-packages, selected distribution names and new public exports. Clear PATH before native execution to exclude Node/Prosaic; use a disposable loopback fixture, synthetic inspected artifact, old one-argument native tool and new contextual journaled tool. Reconstruct the host registry/scope to replay a journal outcome once, force observer failure, apply finite caps and generate an offline conformance report. Assert exact expected HTTP/effect counts and no accounting metadata unless explicitly configured. Use a temporary test-only sqlite journal and hard subprocess/HTTP timeouts, never a production ledger or endpoint.

~~~python
assert sys.flags.isolated == 1
for module in (prosaic, prosaic_runtime):
    assert "site-packages" in Path(module.__file__).resolve().parts
assert importlib.metadata.version("b3-prosaic-runtime") == "0.8.0"
assert callable(prosaic_runtime.evaluate_conformance)
assert callable(prosaic_runtime.tool_journal_descriptor)
os.environ["PATH"] = ""
~~~

Keep Core canonical validation owned by Core/Harness; no new Core dependency appears inside validate_execution_artifact. Adapt acceptance tests to root's candidate wheelhouse with exact lower supported versions, then to downloaded published hashes only after root publishes.

- [ ] **Step 4: Run the full relevant qualification once after code integration.**

Run .venv/bin/python -m pytest -q, with test-only loopback escalation if the sandbox denies local binds. Require native AMD64/ARM64 × Python 3.11–3.13 and macOS sandbox evidence from existing CI; inspect any framework-related skip and preserve the separately mandatory operational sandbox gate. Do not weaken confinement to make a test pass.

Run PYTHONPATH=adapters/postgres/src:tests .venv/bin/python -m pytest adapters/postgres/tests -q -k 'not harness_resume' only with root-provided explicitly owned DSNs. Require the real PostgreSQL 16/18 gate; scope failures/skip counts are included in the acceptance receipt.

In root's clean candidate environment run python -I scripts/production_wheel_smoke.py and PROSAIC_RUNTIME_WHEEL_ACCEPTANCE=1 python -I -m pytest tests/test_package_compatibility.py::test_wheel_metadata -q; the installed metadata gate must pass with zero skips. Root builds wheels/source archives from a clean exact tree and performs payload/hash/index checks; the Runtime implementer supplies evidence, not uploads.

- [ ] **Step 5: Record compatibility evidence and run self-review.**

Record command, Python/platform, candidate distribution identities, public exports, test counts and remaining external qualification in docs/production-acceptance.md. Confirm Result's seven serialized fields, old descriptors, existing accounting scope/quantities, no default new metadata, unchanged low-level provider defaults, no optional observer authority and no mixed old/new installed family. Run git diff --check.

- [ ] **Step 6: Independent review, then commit only R5 acceptance files.**

Stage named tests, smoke script and docs/production-acceptance.md.
Run git commit -m "test: qualify production runtime and adapter compatibility".

## Plan self-review and execution handoff

- [x] Spec coverage: R1 covers optional telemetry and independent scope; R2 covers finite invocation controls; R3 covers namespace-scoped claims, fencing/uncertainty and callback compatibility; R4 covers pure opt-in qualification; R5 covers accounting/adapters/renamed installed packages.
- [x] Ownership: Harness policy/bundle/recovery implementation, business PostgreSQL journal, Core locking/validation and actual packaging/publication remain in their respective plans.
- [x] Interfaces: InvocationScope, ObserverEmitter, ToolExecutionContext, ToolClaim, ToolJournal and tool_journal_descriptor are named once and shared with the Harness/lab planners.
- [x] Review failure modes: all five Review Focus entries have owning tests and concrete assertions above.
- [x] Compatibility: disabled descriptor/identity fields stay omitted; operation scope never enables billing; checkpoint/receipt/ledger formats stay intact.
- [x] No implementation handoff question remains: the human approved the architecture and autonomous parallel execution, explicitly waiving additional design handoffs.

Execute R1–R4 in order with fresh task reviews. R5 is the integration/release qualification gate after root stages final candidate names/dependencies. Public uploads follow the architecture's final synthetic qualification gate, not this plan's completion alone.
