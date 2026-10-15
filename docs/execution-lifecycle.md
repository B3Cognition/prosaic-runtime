# Correlation and cooperative execution drain

Runtime 0.9.5 adds `http_correlation_v1` and `execution_lifecycle_v1`.
Both OpenAI-compatible and native Anthropic HTTP requests carry
`X-Correlation-ID` and `X-Client-Request-Id`. Accounted requests use the
host-generated `provider_call_id`, after durable intent persistence and before
network dispatch. Unaccounted requests use a fresh opaque UUID. Accounted result metadata already
retains these IDs in `accounting_v1.provider_call_ids`; the unaccounted result
contract is unchanged. Provider response IDs remain separate.
These headers convey trace identity, not idempotency, billing proof or retry safety.
A proxy may ignore or strip them; verify forwarding separately in your deployment.
Historical unknown dispatches cannot be reconciled from newly added headers.

Every Runtime owns an `ExecutionLifecycle`; pass `lifecycle=shared` to share one
with a host's admission/persistence boundary. `run()` and `turn()` automatically
track execution through accounting observations and return. RuntimeAdapter exposes
and tracks the same lifecycle, including callback processing between attempts.

```python
from prosaic_runtime import ExecutionLifecycle, ProsaicRuntime

lifecycle = ExecutionLifecycle()
runtime = ProsaicRuntime(config, lifecycle=lifecycle)
# Run synchronous work in application-managed worker threads.
# On shutdown, the host fences admission and withdraws readiness:
lifecycle.begin()
# A separate supervisor thread waits BEFORE closing stores or setting cancellation:
lifecycle.wait()  # Returns True only when all admitted work has exited.
```

`wait(timeout=seconds)` returns False if work remains. It never cancels work,
releases ownership, retries requests, closes storage or kills a process. Keep
waiting when preserving receipts is required. Calling wait from admitted work
raises rather than deadlocking on its own admission. Admission closes permanently;
create a new lifecycle for a new service lifetime.

`with lifecycle.work():` lets a host extend tracking through its own persistence.
Already admitted synchronous work may enter nested scopes after draining starts,
so a multi-step workflow can finish. Nesting is thread-local: it does not grant new
threads admission and is not an async-task scope. Async hosts must keep the complete
synchronous operation in one worker thread and schedule waiting off the event loop.
The SDK installs no signal handlers; servers and supervisors own shutdown ordering
and external termination deadlines. SIGKILL and host loss still leave unknown
outcomes requiring reconciliation. No automatic retry or zero-usage assumption
is introduced.
