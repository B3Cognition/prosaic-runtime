# Tool outcomes and bounded recovery

Runtime exposes immutable `ToolOutcome` evidence on `tool_completed` events and
safe observer records. The version 1 contract contains `code`, `phase`, `effect`,
`recovery` and `replayed`, with a fixed mapping validated by `from_dict`.
It contains no arguments, results, schemas or exception messages.

`RunPolicy(bounded_tool_recovery=True)` opts into stopping after denied authority,
controlled failure, unknown effect or journal failure. Only argument rejection
before handler dispatch permits correction. Runtime never retries a callback.
Workflow owners must preserve their existing request, tool, token and time caps.
Missing, malformed or unclassified evidence cannot authorize another effect.

Handler `ToolFailure` values are completed controlled failures, even when their
code says `invalid_arguments`. Ordinary returned dictionaries are result data.
Handler exceptions, invalid/oversized results and journal uncertainty require
reconciliation. A journal replay is evidence of a recorded completion, not
permission to execute it again. Cancellation and interruption remain terminal.

The default policy preserves existing execution behavior. Model result envelopes
and version 1 journal payloads remain unchanged. Consumers can require capabilities
`tool_outcomes_v1` and `bounded_tool_recovery_v1` before opting in. Structured tool
loops expose the same terminal observer evidence and retain their existing stops.
