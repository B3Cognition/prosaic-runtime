# Host-owned native completion

Runtime 0.9.11 adds an opt-in completion boundary for ProsaicRuntime.run. A
trusted tool handler returns TerminalCompletion(output, artifact=...) after
validating its real result. Runtime retains both values, their digests and the
actual dispatch identity, and stops without another provider acknowledgement.

```python
from prosaic_runtime import CustomTool, RunPolicy, TerminalCompletion

def lookup(arguments):
    artifact = validated_backend_lookup(arguments)
    return TerminalCompletion({'complete': True}, artifact=artifact)

tool = CustomTool('lookup', 'Look up an admitted entity', {
    'type': 'object', 'additionalProperties': False,
}, lookup, 'terminal-v1', terminal=True)
policy = RunPolicy(allowed_tools=frozenset({'lookup'}),
                   allowed_terminal_tools=frozenset({'lookup'}))
```

Retain independent Runtime configuration, agent and host tool grants. The exact
typed result and host policy grant are required. Model/backend fields named
complete, finalArtifact or terminalCompletion confer no authority. A normal model
final response is insufficient when effective terminal grants require the host
result. Ordinary partial JSON from a terminal-capable handler can continue the
loop; failures cannot silently request acknowledgement repair.

The private metadata field terminalCompletion contains the inline artifact and a
versioned binding with invocationId, runId, stepId, toolName, toolVersion,
toolCallId, turn, argumentsSha256, outputSha256 and artifactSha256. artifactIncluded
indicates whether this document contains the artifact. It does not claim an
in-memory Result is durably stored. Critical tool events carry the binding only;
best-effort monitoring never receives the artifact.

RuntimeAdapter preserves the completion binding and refuses further delegate
dispatch after completion or a failed terminal-enabled dispatch that may have
had effects. An adapter cannot rewrite a successful terminal output.

Only singleton terminal batches, inline artifacts and native API tools through
run are supported. Journaled terminal tools, conversation turns, structured JSON
transport, direct run_tool, native CLI agents and unified pools are not enabled
by this capability. Ordinary journaled prerequisite tools remain available.

Cancellation/deadline after a valid handler returns retains known completion
evidence when possible, but the invocation fails with no successful stdout.
Limits can leave only a digest breadcrumb with artifactIncluded=false. Preserve
actual incurred accounting; neither a breadcrumb nor successful tool status is
an accepted result.

Runtime owns execution; hosts own result persistence and admission. Use Harness
0.9.6's receipt-before-validation contract for durable workflows. See the
[Harness integration guide](https://github.com/B3Cognition/prosaic-harness/blob/v0.9.6/docs/terminal-completion.md)
for semantic validation, accepted-only retrieval and restart recovery.

Run the credential-free installed-wheel witness with
`python -I scripts/terminal_completion_smoke.py`. It verifies a real native wire,
one provider request/tool effect, an owned inline artifact and telemetry bounds.
Release-candidate verification is separate from product integration testing.

ALWAYS validate domain completeness before constructing TerminalCompletion.
NEVER treat an execution candidate as durable product success before persistence
and the configured host validators have accepted it.
