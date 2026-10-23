# Unified native/API pools

`UnifiedRuntime` selects Claude Code, Codex CLI and API/local endpoint profiles
for stateless assignments. It delegates execution to the existing native/API
engines. Existing configurations, CLI modes and execution classes keep their
contracts. This opt-in extension does not implement conversation migration.

## Configuration and explicit authority

Use `UnifiedRuntimeConfig.load('unified.yml')` with the shape in
[`examples/unified.yml`](../examples/unified.yml), or construct it from existing
`NativeRuntimeConfig`/`RuntimeConfig` objects, a mapping of `ProfilePool` objects,
a default pool and an explicit API profile-to-quota-key mapping. Either backend
configuration can be `None`; at least one must exist. Profile aliases must be
globally distinct. Shared accounts, including aliases across native/API families,
must use the same quota key. A file reference is resolved relative to the unified
file; the existing backend loaders retain their own path semantics.

The Python API and CLI require permissions from the host on every invocation.
The CLI detects `kind: unified` and requires explicit total timeout, dispatch
limits, and backend policies. For example:

```sh
prosaic-runtime subagents/repair.md --config unified.yml --pool agents \
  --timeout 180 --max-attempts 3 --max-native-attempts 2 \
  --native-permissions workspace-write --native-timeout 60 \
  --enable-api --api-timeout 180 --max-api-requests 8 \
  --max-api-tool-calls 4 --max-tool-rounds 4 \
  --allow-tool write_file --write-path calculator.py
```

Omit native permission flags and set `--max-native-attempts 0` for API-only
execution. Omit `--enable-api` and all API grants/budgets for native-only
execution. API request and tool-call caps accept zero; API reported tokens can
also be capped with `--max-api-reported-tokens`. These caps cannot bound the
vendor-owned native inner loop. The total deadline includes every attempt;
backend deadlines are further limited by remaining time.

`--require-capability NAME` is repeatable. `--events`, `--output json` (default),
`--output text`, and `--quiet` retain their usual output contracts. Exit codes
are 0 for success, 1 for execution failure, 2 for invalid configuration or
authority, and 130 for interruption. Backend result metadata can contain
private data; filter it before sharing. Unified execution rejects `--profile`,
accounting/context flags, and grants for disabled backends. Select `--pool`
instead. Existing native/API configuration modes retain their behavior.

The equivalent Python policy is:

```python
from prosaic_runtime import (
    NativePolicy, PoolPolicy, RunPolicy, UnifiedRuntime, UnifiedRuntimeConfig,
)

runtime = UnifiedRuntime(UnifiedRuntimeConfig.load('/private/unified.yml'))
result = runtime.run(
    inspected_agent, cwd=workspace,
    policy=PoolPolicy(
        timeout_s=180, max_attempts=3, max_native_attempts=2,
        native_policy=NativePolicy(permission_mode='workspace-write', timeout_s=60),
        api_policy=RunPolicy(
            allowed_tools=frozenset({'read_file', 'write_file'}),
            read_roots=(str(workspace),), write_paths=(str(workspace / 'output.json'),),
            timeout_s=180, max_provider_requests=8, max_tool_calls=7,
            max_tool_rounds=8, max_reported_tokens=65536,
        ),
    ),
)
```

An omitted backend policy excludes its members. API request and tool caps must
be explicit, including zero. The total deadline covers inspection, callbacks,
capacity locks and every candidate. Backend timeouts are reduced to the remaining
total time. API request/tool/token counters are carried between API candidates;
native attempts have a separate cap because vendor inner loops are opaque.
API token caps apply only to API execution, not to native subscriptions.

Native permissions are vendor-owned and coarser than API path/tool grants.
Supplying both policies explicitly accepts those independent scopes. Exact API
grants never become native authority. Use `required_capabilities`, for example
`frozenset({'read_receipts_v1'})`, to filter out backends that cannot provide the
required contract. The facade itself advertises only `unified_pools_v1`, not
conversation, receipt or accounting capabilities. `tools: full` excludes API
candidates; custom tools exclude native candidates. The selected pool owns profile
selection, overriding backend default/model-tier routes without editing prose.

## Switching and unavailable pools

The pool prefers the first eligible profile whose quota key is not cooling down.
Unknown capacity is eligible; this is not a concurrent capacity reservation.
There are no implicit probes or server startup. `CapacityStore` can be supplied
by the host, or stored in the configured atomic, locked local file.

Automatic switching needs positive `prework_failure_v1` evidence from the concrete
execution engine. Native exhaustion is safe only before any observed model text,
reasoning or tool work. This proof is stricter than the standalone native pool's
effect-free retry rule. For API endpoints, only a first-request HTTP 429 rejection or
a TCP connection refusal before acceptance can prove this, with zero earlier
tool dispatches. A required first model tool has not run merely because it was
declared; an already dispatched tool prevents fallback. Progress/observer events
never authorize switching. Proof is restricted to direct transport acceptance;
accounting-wrapped transport paths conservatively withhold it. HTTP 401/503, malformed responses, read/connect
timeouts, resets and unknown transport failures stop conservatively.

Failures after API dispatch without safe proof require recovery. Native effects
retain their existing recovery evidence. Cancellation, depleted allowances and
critical callback failures never cause another dispatch. A successful result
failing the consumer's output contract is also not automatically replayed.
When all eligible members are cooling down, the pool promptly returns
`capacity_unavailable` with the earliest `retry_at`; it does not wait or retry.
Quota state is shared across aliases and processes, and expiry restores
eligibility. Neither expiry nor a reset guarantees valid authentication or quota.

## Evidence and limits

`result.metadata['unified_execution_v1']` reports profile attempts, separate
`native_attempts`, `api_provider_requests`, `api_tool_calls`, API reported tokens,
known total tokens and usage completeness. Unknown delegate dispatch counts remain
`None`; unknown native usage does not become zero. Original backend result
metadata remains backend-owned, including its privacy requirements and usage
limits. No subscription/API cost equivalence is inferred.

Pool `on_event` callbacks are critical host hooks with structural selection,
skip/fallback and completion records. They contain no prompts, commands, endpoint
credentials or session IDs. The pool does not merge native/API tool loops,
construct shared sessions or transfer partially executed work. Hosts own workspace
isolation, durable admission/allowances and recovery authorization. It does not
bind a shared accounting recorder or promise one portable native/API ledger.
