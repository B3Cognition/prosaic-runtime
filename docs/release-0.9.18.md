# Runtime 0.9.18 — sent reasoning-effort evidence V1

Returned results now report the effort controls actually encoded for each HTTP
attempt or passed to each launched native CLI. `reasoning_effort_v1` separates
artifact requests, original profile defaults, exact sent fields/format and
unobserved provider confirmation. It retains histories across tool continuation,
rule repair, adapter retries and safe native/API pool fallback. Zero-attempt,
unknown and bounded/truncated histories are explicit. See the [public contract](sent-reasoning-effort.md).

**Compatibility and identity:** This is additive Result metadata and a capability.
No artifact, rule, prepared input, workflow or bundle identity fields change;
independent public 0.9.17 identity goldens remain exact. Saved private Result
receipts gain evidence. Old receipt absence stays unavailable. CompletedTurn,
TurnEvidence, accounting intents/observations and published PostgreSQL recorder
0.3.0/store0.4.1 schemas remain unchanged. Core stays0.5.0 and Harness stays0.9.12.
Consumers comparing whole Result metadata should accept the versioned extension;
per-attempt correlation IDs intentionally differ across invocations.

**Limits:** Sent means an attempted HTTP dispatch or launched process, not
successful delivery, provider acceptance or compliance. Native inner calls and
vendor settings remain opaque. No new effort support, retry allowance or live
provider certification is introduced. Trace rows/bytes and caller metadata caps
bound evidence without changing execution authority. No raw prompts/headers,
credentials, workspace paths or unrelated argv are copied into the extension.

**Validation and publication:** Required qualification covers fresh exact source,
isolated installed wheels/smokes, both credential-free synthetic wires, fake
native processes, retry/failure/copy/bounds/identity tests, dependency floors and
real PostgreSQL16/18 gates. Only original successful CI-qualified bytes with
Core0.5.0 lineage may be promoted. The release page supplies actual qualification
and public-download evidence; a planned command is not a passing test. The Lab
blueprint is qualified and released separately against actual public SDK bytes.
