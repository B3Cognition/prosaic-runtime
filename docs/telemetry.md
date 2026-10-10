# Optional observation

`ProsaicRuntime.capabilities` includes `observer_v1`. Supply a synchronous
`observer(record)` to `run` to receive owned dictionaries with `version=1`,
`sequence`, `source="runtime"`, `event` and opaque scope references. Each invocation
has its own sequence and ContextVar state; nested or concurrent invocations do
not inherit another invocation's observer.

```python
from prosaic_runtime import InvocationScope

records = []
result = runtime.run(artifact, observer=records.append,
    operation_context=InvocationScope("attempt-7", run_id="run-2", step_id="step-3"))
```

`InvocationScope` is frozen and has `invocation_id`, optional `run_id`, `step_id`
and `operation_namespace`. Supplied values must be plain nonempty strings at most
128 UTF-8 bytes, without Unicode control, format or surrogate characters. When
omitted, Runtime generates a UUID invocation ID. Scope is independent of
`ExecutionContext`; it does not enable accounting or resolve billing defaults.
Invalid scope fails before callbacks or execution.

Runtime emits `invocation_started`, `invocation_completed`,
`provider_request_started`, `provider_request_completed`, `tool_started` and
`tool_completed`. Provider observations surround the HTTP response context,
including read and close failures. They report transport completion; successful
transport does not guarantee valid provider content. Invocation completion is
the execution outcome: `completed`, `cancelled`, `timed_out`, `admission_failure`,
`critical_hook_error`, `budget_failure` or `execution_failure`. The public wrapper
delivers one terminal record for returned Results and escaped admission or
critical-hook failures, independently of cancellation checks.

Records contain only allowlisted scalars: opaque references, SHA-256 digests,
nonnegative finite counts/durations, exit/HTTP status and closed outcome, reason
and tool status sets. Tool names are host-registered names or `unknown` for
unregistered model names. Request IDs, arbitrary tool versions, raw arguments,
read receipt paths, prompts, model output, previews, endpoint URLs, headers,
credentials and exception messages are omitted. Invalid dynamic fields and
unrecognized fields/events are dropped. Counts are bounded to signed 64-bit
nonnegative integers; HTTP status is 100–599 and exit code 0–255.

`ObserverEmitter(observer, source="harness", scope=scope, labels=None)` is also
exported for shared host delivery. Its `emit(event, **fields)` accepts Harness
events `run_started`, `transition_committed`, `waiting_committed`,
`recovery_committed`, `blocked_committed` and `run_completed`. Harness supplies
the actual committed `outcome` (`running`, `waiting`, `blocked`, `completed` or
`rejected`), opaque/nonnegative integer `revision` and nonnegative integer `calls`.
These fields survive size bounding. A host may explicitly supply at most 16
public labels; plain nonempty keys/values have limits 64/128 UTF-8 bytes and the
same character restrictions as scope. Labels are copied and removed first when
needed to keep serialized records within 4096 UTF-8 bytes. Never put private
information in public labels or opaque references.

The observer receives a fresh owned record. Mutating it cannot mutate critical
events, future records or execution results. Ordinary `Exception` failures are
isolated; `ObserverEmitter.failures` counts them without saving their text.
Process-control `BaseException` failures such as `KeyboardInterrupt` propagate.
Callbacks are synchronous and must remain fast: Runtime cannot preempt a hung
callback. Callbacks during execution consume the remaining wall-clock allowance.

`on_event` retains its existing critical, propagating behavior and original
payloads, including read receipts and text events. Use it for evidence whose loss
must stop execution. Optional observation is best effort, without durable delivery,
retries or an outbox; it supplies no authorization, persistence or billing proof.
Pure `validate_execution_artifact` never constructs an observer or executes a
callback.
