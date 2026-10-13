# Bounded host invocation adapters (Runtime 0.9.1)

`RuntimeAdapter` is a public boundary for trusted host argument projection,
structured transport orchestration, bounded retries and output normalization.
It wraps an unmodified, concrete `ProsaicRuntime`; overriding its `run` method
or using a subclass is rejected. Ordinary `ProsaicRuntime.run()` is unchanged.

```python
from dataclasses import replace
from prosaic_runtime import RuntimeAdapter, RunPolicy

# runtime is an operator-configured ProsaicRuntime with registered host tools.
def invoke(artifact, arguments, *, execute, **options):
    result = execute(arguments, allowed_tools=frozenset(), max_tool_rounds=1)
    return replace(result, stdout=host_normalize(result.stdout)) if result.exit_code == 0 else result

adapter = RuntimeAdapter(runtime, version='host-projection-v1', invoke=invoke,
                         max_attempts=3)
result = adapter.run(agent, request_json, policy=RunPolicy(
    timeout_s=30, max_provider_requests=3, max_tool_calls=0,
    max_reported_tokens=32768))
```

The hook receives the approved artifact, original arguments, incoming policy,
event/cancellation callbacks and original invocation options. `execute(projected,
**reductions)` is its only supported dispatch boundary. Reductions are
`timeout_s`, `allowed_tools`, `max_tool_rounds`, and `max_input_bytes`; tools must
be a subset of incoming grants, and numerical limits are clamped downward.
The artifact, credentials, workspace, observer, operation scope and other
Runtime options stay bound to the original invocation. Events go to the original
critical event callback. Argument hashes distinguish projected attempts without
retaining their text in adapter metadata.

A single absolute deadline and cumulative provider/tool/reported-token allowance
span all executions inside the adapter call. Every actual Runtime result is
snapshotted before returning it to the hook. Before another execution, the
remaining policy deducts prior requests, tools and known usage. Unknown usage
blocks another request when a reported-token cap is present. Attempt count is
bounded to 1–8 (default 3), including zero-provider Runtime attempts.

The adapter never automatically retries. The host must explicitly classify a
retry as appropriate and must not repeat an uncertain tool effect or interrupted
provider operation. A retry cannot reset the allowance. Sleep/projection time
consumes the original deadline; cancellation and expiry are rechecked before
execution and successful return.

The final `Result` retains aggregate known usage/cost evidence, while total token
usage stays `None` if any dispatched attempt has incomplete evidence. Known
partial usage remains in `invocation_budgets_v1.reported_tokens`. Bounded
`runtime_adapter_v1.attempts` records per-attempt usage, counts, status and projected
argument hash. A callback failure returns a safe failure with incurred evidence;
it cannot erase earlier dispatches or manufacture a successful result from an
unsuccessful final Runtime call. A successful result requires an actual successful
Runtime execution. Normalization may change the admitted host output, but does
not change observed usage or dispatch counts.

Hooks are synchronous trusted Python code, like host tools/validators. They are
not an OS sandbox and cannot be forcibly preempted. Host code must remain
cooperative and must not bypass `execute` with its own model calls. Adapter
versions identify host semantics; bump them when projection/retry behavior changes.
Accounting, conversation state and database storage are independent and are not
required or enabled by this API. Use Runtime 0.9.1 or later and Harness 0.8.1 or later for admitted workflows.
