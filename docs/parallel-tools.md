# Parallel read tools

Runtime 0.9.21 adds opt-in concurrency to API `run` and role-aware
`turn`/prepared-turn execution on OpenAI-compatible and Anthropic providers.
The default remains sequential. Direct `run_tool`, native/CLI backends and the
external structured loop do not support a parallel policy. MCP clients and
sub-agents remain separate follow-ups.

Register trusted custom handlers with `with_context=True, parallel_read=True`.
This is a host assertion that the handler is read-only, independent of siblings,
and safe to call concurrently, including concurrent calls of itself. Tool
schemas, model output and descriptions cannot grant this property. Terminal
handlers cannot declare it. Supply explicit `ToolContractBindingsV1`, finite
provider/tool caps, and `RunPolicy(parallel_tools=ParallelToolPolicyV1(...))`.
Typed returns and required citations are optional, independent features.

The coordinator reserves the complete admitted batch atomically, runs contiguous
eligible reads in fixed windows up to `max_concurrency`, then reduces results in
wire order. Unmarked custom tools, builtins, context readers and CLI tools are
serial barriers. A window is settled and reduced before the next starts; there
is no refill as individual handlers finish. Pending reservations protect shared
budgets, while actual tool counters count consumed attempts only. Authorization,
journals, guards, evidence, observers and sinks run on the invocation owner.
Workers receive fresh context and may invoke only their registered handler;
Runtime reentry is rejected. Every started handler is joined before return.

Cancellation and faults stop further dispatch, drain started handlers and retain
known completions. Python callbacks must cooperate with cancellation; there is
no hard timeout or forced thread termination for an uncooperative callback.
Same-key same-signature local journal work waits then replays with current
permission; conflicting or foreign uncertain work is never blindly retried.
There is no exactly-once guarantee for external effects.

`tool_execution_settled` reports physical settlement with a closed, payload-free
`parallelToolV1` object. It contains correlation, status and duration only.
It does not certify guard acceptance or provider delivery. Existing approved
`tool_completed` projections, guard facts, history and evidence reduce in wire
order. Evidence becomes citable only after actual encoded provider delivery and
current permissions. Settlement observations grant no authority.

| Execution | Encoding | Frozen input | Turn evidence |
| --- | --- | --- | --- |
| Existing ordinary/view/context turns | unchanged v1–v3 | unchanged v1 where supported | unchanged v1–v2 |
| Existing tool contracts | conversation-turn-v4 | 2 | 3 |
| Explicit parallel policy | conversation-turn-v5 | 3 | 4 |

Enabled documents require `parallelToolsV1`, which pins scheduler
`independent-read-windows-v1` and all policy bounds. Restore rechecks the host
policy and registered tool descriptors, including handler version/read assertion.
Old documents do not acquire concurrency. CompletedTurn, ToolOutcome and tool
journal formats are unchanged.

Build once from a committed head using `scripts/release_artifacts.py build
--ref <commit> --output <new-directory> --expect
b3-prosaic-runtime=0.9.21`. Keep its original artifacts and receipt. Install
that wheel into an isolated environment with Core 0.5.0 and test dependencies,
then run `python -I -m pytest -o pythonpath=tests
 tests/test_parallel_tool_installed.py` and `python -I
 scripts/tool_contract_smoke.py`. No publication is part of this checkpoint.
