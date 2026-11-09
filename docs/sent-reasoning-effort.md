# Reasoning effort actually sent — V1

Runtime 0.9.18 adds `reasoning_effort_v1` to runtime capabilities and to returned
`Result.metadata`. It describes transport attempts, not model behavior:

```python
result = runtime.run(agent, policy=policy)
trace = result.metadata.get('reasoning_effort_v1')
if trace is not None:
    for attempt in trace['attempts']:
        print(attempt['requested_effort'], attempt['configured_effort'],
              attempt['format'], attempt['sent'])
```

The document has `version: 1`, `attempt_count`, `complete`, `omitted_attempts`
and ordered `attempts`. Each row contains `index`, `transport`, `backend`,
`profile`, `model`, `provider_call_id`, `requested_effort`, `configured_effort`,
`format`, `sent` and `provider_confirmation: 'not_observed'`.
Requested effort comes from the artifact; configured effort is the original
profile default. Neither annotation proves that a corresponding field was sent.

HTTP rows are captured from the final encoded request at the attempted opener
handoff, after admission, cancellation, accounting `prepare` and budget checks.
Connection, HTTP, read and accounting `observe` failures retain attempted rows;
a failure before dispatch does not invent one. `provider_call_id` is the actual
Runtime correlation ID, so a host can join it to existing recorder calls. This
release does not change accounting intent/observation schemas or recorder DDL.
A row does not prove successful delivery, provider receipt or compliance.

Only these HTTP controls are copied:

| `format` | Actual `sent` fields |
| --- | --- |
| `reasoning_effort` | `reasoning_effort` enum string |
| `thinking_toggle` | `chat_template_kwargs.enable_thinking` boolean |
| `chat_template_effort` | Both fields above |
| `omitted` | Empty object |
| `unavailable` | Empty object; observation incomplete |

Explicit `none` or `false` differs from omission. Binary thinking enabled does
not imply a neutral effort tier. Current Qwen `none` sends only a false thinking
toggle. Anthropic effort support is unchanged: ordinary supported requests show
omission. No new provider/model effort levels are added.

Native rows describe only a process that actually launched. `sent.argv` contains
only `['--effort', value]` for Claude Code (`claude_cli`),
`['-c', 'model_reasoning_effort="value"']` for Codex (`codex_cli`), or `[]`
(`omitted`). Metadata discovery, capacity skips and startup failures are not
model attempts. Native `provider_call_id` is null. Vendor settings and inner CLI
provider requests remain unobserved. No prompt, header, environment, executable,
workspace, credentials or unrelated command arguments are copied into this trace.
Profile/model labels must be printable and at most 256 UTF-8 bytes, else null.

`run`, `turn`/`turn(PreparedTurn)`, NativeRuntime, UnifiedRuntime and RuntimeAdapter
retain invocation-local owned histories across tools, repairs, retries and safe
fallback. Model-free `run_tool` explicitly reports zero attempts. Unknown delegate
execution yields null counts and `complete: false`, preserving earlier observed
rows. A hook cannot replace the collector's evidence with fabricated metadata.

The extension has at most 64 rows and 32,768 canonical JSON bytes. Truncation sets
`complete: false` and an explicit omitted count; unknown total/omission counts are
null. Caller turn metadata/depth/node limits may trim rows further, or omit the
extension if its minimal envelope cannot fit. Indices order retained observations,
not absolute provider-call ordinals; a truncated child may leave a gap before a
later retained aggregate row. Absence means unavailable, never
zero or an invented historical setting. Limits bound observation, not execution.

CompletedTurn and TurnEvidence schemas, artifact/rule/prepared/workflow/bundle
input identities and accounting hashes are unchanged. Private saved Result
receipts gain additive metadata; published Harness 0.9.12 retains it without a
new store schema. Old receipts remain readable without invented traces. Hosts
must filter operator/private receipts before publishing application views.

[Lab0.9.11](https://github.com/B3Cognition/prosaic-integration-lab/releases/tag/v0.9.11)
provides the [copyable sent-effort blueprint](https://github.com/B3Cognition/prosaic-integration-lab/blob/v0.9.11/docs/sent-reasoning-effort.md)
and complete Core0.5.0/Runtime0.9.18/Harness0.9.12 qualification, including actual
old-receipt recovery. Its release evidence distinguishes synthetic controls,
real PostgreSQL persistence and public distribution checks from live providers.
