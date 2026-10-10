# Trusted tool context and effect journals

`CustomTool(..., with_context=True)` explicitly opts the handler and any supplied
authorizer into `(arguments, ToolExecutionContext)`. Ordinary registrations retain
their one-argument callbacks and exact existing descriptors. Enabled descriptors
add `with_context: true`; supplying `operation_key` also adds `journaled: true`.

The frozen context contains `scope: InvocationScope`, `tool_name`, `tool_version`,
zero-based `call_index`, `arguments_sha256`, the absolute monotonic `deadline`, a
`cancelled()` callback, and an optional `operation_key`. The operation scope is
host authority and is independent of billing/accounting `ExecutionContext`.
Runtime creates a fresh invocation scope when none is supplied. Journaled tools
require the host to supply a scope with `operation_namespace` and a journal.

The model supplies only schema-validated arguments. It cannot choose or override
the scope, resolver, key, journal, deadline or authorization identity. Authorizers,
resolvers and handlers each receive an independent argument copy. The authorizer
and resolver see `operation_key=None`; only after authorization does the host key
resolver run, and the handler sees its validated key. Grants and argument schemas
also apply to journal replays.

```python
from prosaic_runtime import CustomTool, InvocationScope, RunPolicy

tool = CustomTool(
    'reserve_order', 'Reserve the approved order', parameters, reserve_order, 'v1',
    with_context=True,
    authorize=authorize_order,
    operation_key=lambda arguments, context: trusted_order_operation,
)
result = runtime.run(
    artifact, policy=RunPolicy(allowed_tools=frozenset({'reserve_order'})),
    operation_context=InvocationScope(
        'attempt-2', run_id='run-17', operation_namespace='tenant-order-ledger'),
    tool_journal=host_journal,
)
```

`trusted_order_operation` comes from trusted host state. Provider call IDs,
invocation ordinals and argument hashes are correlation/signature data; Runtime
never converts them into business idempotency keys. Equal keys in separate trusted
namespaces address separate operations. Namespace/journal identity belong in the
host's sealed native binding. `CustomTool.version` versions handler, authorizer,
captured fixed data and resolver semantics together; change it when those semantics
change.

## Host journal contract

`ToolJournal` declares plain `identity: str` and
`contract_version='tool-journal-v1'`, plus:

```python
claim(operation_namespace, operation_key, signature) -> ToolClaim
commit(operation_namespace, operation_key, claim_token, bounded_outcome) -> None
```

`tool_journal_descriptor(None)` returns `{}`. For a journal it returns only
`contract_version` and `identity`. Admission inspects static declarations; it
rejects properties and dynamic descriptors without executing them, probes or
journal callbacks. Identity and namespaces are bounded opaque references of at
most 128 UTF-8 bytes, with control, format and surrogate characters rejected.
Keys and fencing tokens have the same validation with a 256-byte bound. Use a
stable logical identity, never a physical path, DSN, object ID or callback source.
The host must retain the same durable ledger under that identity across deployments
and credential changes.

The SHA-256 signature covers tool name, tool version and validated arguments,
using sorted, compact, finite UTF-8 JSON (`ensure_ascii=False`). The argument
digest uses the same encoding. A claim must be exactly `ToolClaim` with the
matching lowercase 64-hex signature and one of these closed states:

| State | Permitted fields | Runtime action |
| --- | --- | --- |
| `new` | Bounded nonempty `claim_token`; no outcome | Dispatch the effect once. |
| `replay` | No token; finite bounded `{'status': 'ok', 'result': ...}` outcome | Copy and return the recorded outcome. |
| `uncertain` | No token or outcome | Stop pending explicit host resolution. |

The journal must atomically claim the namespace/key address and commit using a
compare-and-set against the current fencing token. Stale tokens must never
overwrite a recorded or reconciled outcome. Runtime rechecks replay outcomes
against the current result limit and does not execute a replayed handler.

After a new claim, Runtime dispatches the handler, validates and bounds its
successful result, commits an owned outcome copy, then checks cancellation/deadline.
A cancellation observed immediately after a successful effect still leaves its
valid outcome committed. Synchronous callbacks are trusted code and cannot be
preempted; they must enforce their own transport timeouts and business fencing.

## Uncertainty and recovery

`tool_effect_uncertain`, `tool_journal_failed` and `tool_identity_conflict` are
fixed critical failure codes. Runtime returns a failed `Result` preserving known
completed-turn usage. Structured transport raises `StructuredToolError` with the
same reason. These failures never enter model correction, forward host exception
messages, or automatically retry.

An exception from a dispatched journaled handler, a nonfinite/non-JSON result or
an oversized post-effect result leaves the claim uncertain. Runtime never commits
an ordinary tool-error envelope for those effects. A failed commit stops the
current invocation. If the journal durably wrote the outcome before its
acknowledgement was lost, a subsequent matching claim can replay that outcome;
an uncertain claim always blocks redispatch.

A journal does not make an external business effect and its recording atomic.
Domain tools must implement transactional/idempotent operations using the trusted
key, or retain uncertainty for authoritative operator resolution. Runtime provides
no reconciliation API, lease expiry, TTL reclaim or automatic retry. Permission to
retry an interrupted model request does not resolve a business effect. The host
may permit effect retry only with authoritative no-effect/quiescent-worker evidence
or domain idempotency/fencing.

The SQLite journal under `tests/support/` is test support only. Production business
journals belong to host applications; usage recorders and Harness checkpoints are
separate stores and responsibilities.
