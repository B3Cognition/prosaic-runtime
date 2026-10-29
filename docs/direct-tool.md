# Model-free custom tool execution

`ProsaicRuntime` advertises `direct_tool_v1` and exposes:

```python
result = runtime.run_tool(
    'lookup', {'value': 2},
    policy=RunPolicy(allowed_tools=frozenset({'lookup'})),
    operation_context=scope,
    context=trusted_context,
)
```

The name must identify a registered host `CustomTool` and be granted by both
Runtime configuration and the host `RunPolicy`. Registration alone grants no
authority. Builtin and CLI tools are unsupported. Arguments must be a literal
JSON object, checked against the tool schema, tool argument bound and policy
input bound before dispatch. `cwd` is accepted for API compatibility; it does
not confer filesystem authority or change the process working directory.

A successful `Result.stdout` contains JSON of the actual callback value, without
a status envelope or model rewrite. `ToolFailure` and execution failures return
a nonzero exit code and a safe `metadata['failure_reason']` identifier.
Result and argument limits, authorizers and `ToolOutcome` classification use
the same custom tool machinery as model-driven execution.

Every result has `token_usage=0`, `cost_usd=0`, and
`metadata['direct_tool_v1'] == {'model_free': True, 'provider_requests': 0}`.
No provider is constructed, called or assigned accounting intents/observations.
The resolved host accounting context still reaches contextual authorizers and
handlers, and is included in `accounting_v1` metadata with no provider call IDs.
Invocation budget metadata records the attempted tool dispatch. Enclosing
`ExecutionBudget` tool allowances and deadlines apply; provider counters remain
unchanged, and existing token stop thresholds remain in force.

The call participates in execution lifecycle drain, observer invocation events,
and version 1 `tool_started`/`tool_completed` events. Events omit callback
arguments and values. Ordinary observer errors are best effort; critical event
sink failures stop execution and produce a safe failure. Tools are trusted,
cooperative callbacks: cancellation/deadlines are checked at boundaries and
cannot preempt arbitrary callback code.

Journaled tools require an `InvocationScope` operation namespace and host
`ToolJournal`. Completed receipts replay the saved actual result; uncertain
claims stay fenced. The Runtime performs no repair or retry. A caller must
reconcile unknown effects before explicitly starting further work. This API
does not grant workflow-level direct execution permission; orchestrators must
separately admit their direct tool steps.
