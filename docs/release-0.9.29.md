# Runtime 0.9.29

Successful tool results can now be sent to OpenAI-compatible and Anthropic
endpoints as TOON instead of JSON. The profile setting is opt-in; JSON remains
the default. Uniform rows can use fewer input tokens, while deeply nested or
irregular results can be larger. See the [format contract](tool-result-format.md).

Install the optional dependency and enable it on the endpoint profile:

```sh
python -m pip install 'b3-prosaic-runtime[toon]==0.9.29'
```

```yaml
profiles:
  local:
    features:
      tool_result_format: toon
```

The change applies only to tool-result text in provider requests. Tool arguments,
journals, guards, result contracts, progress events and conversation records
keep JSON. Error results remain JSON, and results that cannot be encoded and
read back as equivalent data fall back to their original JSON. Input data,
context packs and native CLI backends are unaffected.

Evidence use is reauthorized before dispatch. Delivery receipts and citations
match the actual request bytes, and compacted or unsent tool results are not
marked delivered. TOON decoding is limited to tool-result content; ordinary user
text that resembles TOON or deeply nested JSON does not invalidate a request.
Request byte/token limits apply to the payload sent; `max_result_bytes` still
bounds the internal JSON result and is not an upper bound on TOON text size.

Default request bytes and existing stored record formats are preserved. Selecting
TOON changes the provider request representation and its prepared-turn bindings.
Keep the selected format unchanged while executing or restoring prepared/frozen
turns; changing it is refused with `bindingMismatch` before dispatch.

Applications built on Prosaic Harness need the Runtime upgrade, the `toon` extra
and the profile setting. They need no TOON codec or changes to tool handlers,
validators, journals or conversation schemas. Custom configuration filters must
allow the setting, and consumers of raw provider requests must expect TOON text.
Compatibility tests cover released Harness 0.9.12 execution, pause/reopen,
journal replay and recovery after a lost receipt acknowledgement. That Harness
release cannot forward tool-contract bindings into a workflow step, so evidence
and citation integration is separately tested through Runtime.

Core 0.5.0 and the independently published PostgreSQL recorder 0.3.0 retain their
versions and storage contracts. This Runtime release does not publish a new
recorder or change its schema. The CLI confinement and guarded-recovery behavior
from Runtime 0.9.27/0.9.28 remains in effect.

Qualification covers both API wire protocols, default/error/fallback behavior,
evidence revocation and receipt digests, citations, compaction, conformance,
prepared/frozen turns, Harness recovery and isolated installed-wheel consumers.
The release workflow also requires the existing OS/Python matrix, PostgreSQL
16/18 matrix and dependency-floor gates. Original qualification receipts and
checksums accompany the [versioned release](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.9.29).
These synthetic checks do not certify an untested live model's comprehension,
token savings or deployment capacity.

TOON support was contributed by [Pramoth Natarajan](https://github.com/Pramoth-Natarajan)
in [PR #42](https://github.com/B3Cognition/prosaic-runtime/pull/42).
