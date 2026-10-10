# Deterministic provider conformance

`conformance_v1` provides a closed host assertion report for `text-tools-v1`.
It measures seven required checks in this fixed order:

| Check | Passing observation |
| --- | --- |
| `text_complete` | Successful nonempty text with a confirmed complete finish. |
| `stream_terminal` | Actual streaming and complete finish; OpenAI SSE must contain a recognized `[DONE]` event, native Messages must contain `message_stop`. Text mentioning markers is insufficient. |
| `required_tool` | Enforced first native `conformance_lookup` call, exactly one successful synthetic handler invocation, and complete follow-up. |
| `arguments_validated` | The host rejects the deliberately invalid tool arguments with `invalid_arguments`, invokes no handler, and completes the follow-up. An unrelated tool denial cannot pass. |
| `strict_json` | Complete output parses as finite JSON with unique keys, exactly `{"value":7}`, and an integer value. Requested JSON mode alone is insufficient. |
| `usage_complete` | Executed provider request with complete reported usage; earlier unknown usage stops the suite. |
| `cancellation_boundary` | A synthetic read-only handler runs once, triggers cancellation, and the Runtime returns cancelled with exactly one provider request and no follow-up. |

Every check is required. Explicitly disabled streaming or JSON mode reports the
corresponding check as `unsupported/not_requested`; it cannot qualify the full
suite. With no JSON-mode setting, the suite still measures returned JSON using
its local predicate. Native Messages does not offer the OpenAI JSON-mode control.
This suite measures response behavior, not provider-side schema enforcement or
model answer quality. Smaller capability suites require a future suite version.

## Default and opt-in execution

```sh
prosaic-runtime conformance --config runtime.yml --profile small
# Explicitly authorize bounded provider calls, which may incur provider charges:
prosaic-runtime conformance --config runtime.yml --profile small --live
```

The default returns `not_run/not_executed`, origin `unexecuted`, and
`qualification=not_qualified`. It performs no Runtime construction, discovery,
credential loading, HTTP, CLI/version probes, tool callbacks, or observer calls.
Exit 0 means the default report was produced. With `--live`, exit 0 requires
`qualified`; incomplete, unsupported, or unknown evidence exits 1. Invalid
configuration or policy exits 2. Existing doctor and smoke commands retain their
contracts. `--events` additionally exposes the separate safe Runtime observer
stream; its timings and invocation IDs are outside the canonical report.

```python
from prosaic_runtime.diagnostics import conformance

report = conformance(config, "small")
# For an owned, credential-free localhost fixture only:
fixture_report = conformance(config, "small", live=True, evidence_origin="fixture")
```

Opt-in execution uses already inspected synthetic artifacts, a private temporary
directory, and one synthetic read-only CustomTool. It never inspects caller
artifacts, calls doctor, or discovers configured CLI tools. The suite forwards
only streaming, stream-options, JSON-mode, and reasoning/effort/thinking request
controls from endpoint features. It does not enable arbitrary transcript, file,
or web features. Authentication references are used only during opt-in execution.
Provider responses and input conversations are capped at 65,536 bytes (or a
smaller caller/endpoint limit). The synthetic tool has 1,024-byte input/output
caps. Raw provider data is not retained in the report.

The default shared allowance is `timeout_s=60`, `max_provider_requests=12`,
`max_tool_calls=4`, `max_reported_tokens=32768`, `max_input_bytes=65536`, and
`max_tool_rounds=2`. A caller-supplied `RunPolicy` must supply finite request,
tool-call, and token caps. One absolute suite deadline includes setup; every
scenario receives the remaining time and count/token allowances. Unknown usage
halts later dispatch and records `usage_complete=unknown/usage_unknown`.
Exhausted allowances leave remaining checks `not_run`. Tool-round limits apply
to each scenario. These are reported-usage controls, not a provider cost promise.

## Pure replay and canonical serialization

```python
import json
from prosaic_runtime import evaluate_conformance

fingerprint = "a" * 64
report = evaluate_conformance("text-tools-v1", fingerprint, {
    "text_complete": {
        "state": "passed", "reason": "complete", "origin": "fixture",
        "suiteId": "text-tools-v1", "profileFingerprint": fingerprint,
    },
})
canonical = json.dumps(report, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":"), allow_nan=False).encode("utf-8")
```

The envelope has `version=1`, `suiteId`, public source `runtimeVersion`,
`profileFingerprint`, ordered `cases`, and `qualification`. There are no
timestamps, timings, response IDs, raw prompts/outputs, model names, endpoint
URLs, headers, or credentials. Identical assertion evidence produces identical
canonical bytes. Source version is `prosaic_runtime.__version__`; clean-wheel
qualification separately verifies it against `b3-prosaic-runtime` metadata.

Each imported assertion requires `state`, `reason`, `suiteId`,
`profileFingerprint`, and `origin` (`fixture` or `live`). Optional digest fields
are `artifactSha256`, `argumentsSha256`, and `outputSha256`; optional counts are
`providerRequests`, `toolCalls`, `reportedTokens`, and `handlerCalls`. Digests are
lowercase 64-character SHA-256 strings and counts are nonnegative integers up to
2^63-1. Unknown fields/cases/suites, incompatible success reasons, foreign
suite/profile bindings, invalid origins, nonfinite JSON, cycles, excessive depth
(16), or evidence/report sizes over 65,536 UTF-8 bytes raise `ValueError`.
Missing cases receive `not_run/not_executed` with origin `unexecuted`.

States are `passed`, `failed`, `unsupported`, `unknown`, and `not_run`. Closed
reasons are `complete`, `incomplete`, `not_requested`, `not_executed`,
`unsupported_feature`, `invalid_arguments`, `invalid_output`, `usage_unknown`,
and `cancelled_before_followup`. Successful argument rejection uses
`invalid_arguments`; successful cancellation uses `cancelled_before_followup`;
other successes use `complete`.

The versioned profile identity hashes provider, model, features, temperature,
token/response-byte settings, and base URL identity, excluding credentials and
credential references. Only the digest is exposed. The suite additionally
applies its documented synthetic isolation and bounds. Imported assertions must
carry the exact expected profile fingerprint.

All seven required cases passing with origin `live` yields `qualified`.
All seven passing with origin `fixture` yields `fixture_passed`. Mixed origins,
failed, unsupported, unknown, or missing checks yield `not_qualified`. Origin and
imported assertions are trusted host declarations, not cryptographically
verified provider attestations. Local fixture success never certifies an
untested live model, deployment, or capacity. This implementation's acceptance
evidence uses only owned synthetic fixtures; no paid provider is certified.
