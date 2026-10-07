# Structured JSON tool transport

`StructuredToolLoop` is an explicit alternative to native provider function
calling. It accepts the same registered `CustomTool` definitions, uses their
argument schemas, authorizers and execution/result bounds, and yields one JSON
model input at a time. It never falls back between transports automatically.

The model must return exactly one of:

```json
{"tool":{"name":"lookup_catalog","arguments":{"sku":"SKU-001"}}}
```

```json
{"proposal":{"summary":"A result for the host to validate"}}
```

The host supplies a callable `admit(proposal)`. Return `Completion(value)` for
an admitted result, `ToolRequest(name, arguments)` for a prerequisite read, or
raise `ValueError` for one bounded correction. The optional
`tool_outcome(name, result)` callback can return `Completion` for product-specific
terminal outcomes, such as missing data. Runtime does not interpret application
fields, know about UI components, or approve application writes.

```python
import time
from prosaic_runtime import Completion, RunPolicy, StructuredToolLoop

# artifact and runtime are inspected/configured by the host. Registration is not a grant.
allowed = set(artifact.frontmatter['tools']) & runtime.config.allowed_tools & host_grants
loop = StructuredToolLoop(
    registry, arguments={'prompt': prompt}, allowed_tools=allowed,
    deadline=time.monotonic() + 30, cancelled=cancelled, on_event=progress,
)
turns = loop.turns(admit=lambda proposal: Completion(validate_application_result(proposal)))
arguments = next(turns)
while True:
    # Disable native calls: there is only one execution path for this transport.
    result = runtime.run(artifact, arguments, policy=RunPolicy(
        timeout_s=max(0.001, loop.deadline - time.monotonic()),
        allowed_tools=frozenset(), max_tool_rounds=1,
    ), cancelled=cancelled)
    try:
        arguments = turns.send(result)
    except StopIteration as done:
        application_result = done.value
        break
```

Harness adapters can use the same generator: each agent invocation supplies one
Runtime `Result`, while Harness continues to own routing, checkpoints and workflow
advancement. Register the same descriptors in the Harness workflow so its
fingerprint and adapter checks include the tool contracts.

## Bounds and authority

- Default: twelve model turns and one correction, sharing an absolute monotonic
  deadline. The loop checks cancellation and deadline before/after host execution;
  synchronous callbacks must enforce their own transport timeout.
- Duplicate JSON keys, nonfinite values, excessive depth, unknown tools, invalid
  arguments and extra envelope fields are rejected. No malformed request reaches
  a tool handler.
- Default maximums: 192 KiB input, 128 KiB response, 128 KiB accumulated history.
  Individual argument/results use their registered tool limits.
- Identical tool/argument calls execute once by default. A repeat gets correction
  feedback referencing the existing history; another invalid/repeated turn fails
  closed. Explicit polling applications may raise `max_identical_calls`.
- Progress exposes event types and registered tool names, never arguments/results.
  Histories contain application data: keep them private. Supply credentials in
  callback closures, never in the model arguments.
- Errors carry stable codes without provider/handler exception messages. The
  generator is single-use and performs no provider retry, persistence or model
  selection. Hosts retain their existing retry and consent policies.

Native calling remains available through `ProsaicRuntime.run`; neither the native
implementation nor its compatibility guarantees change with this addition.
