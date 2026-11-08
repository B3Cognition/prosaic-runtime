# Accounting request metadata V1

Date: 2026-11-08. The maintainer explicitly selected Cerebro backlog item3 and
requested implementation, testing, merge and release, including a Lab blueprint
and notes. Native execution follows the established Runtime/consumer boundary.
Routine interface/limit choices below are builder decisions, not claimed quotes
from the maintainer. No live inference or Cerebro application edits.

## Intent and scope

A trusted host can label every accounted provider request with a benchmark run,
variant or similar small diagnostic label. Its custom `prepare(intent)` sees the
labels before dispatch, including model/tool continuations and rule repair. It
need not receive the prompt, request body, tool arguments, headers or credentials.
Existing intent fields already provide requested model, profile, artifact and
resolved run/step attribution; V1 adds labels, not raw request inspection.

Use the existing host accounting API:

```python
context = ExecutionContext(
    run_id='benchmark-42',
    request_metadata={'run': 'benchmark-42', 'variant': 'cold-cache'},
)
runtime.run(artifact, arguments, context=context, accounting=recorder)
# The same context works with turn/prepared turn and Harness(context=...).
# prepare(intent) reads intent['context']['request_metadata']['labels'].
```

`ExecutionContext` and `ResolvedContext` gain optional `request_metadata`.
Constructors accept a mapping of strings and own an immutable snapshot immediately.
Public field access is a read-only mapping. `None`/empty mapping normalize to
absence. The serialized resolved context adds only when nonempty:

```json
{"request_metadata":{"version":1,"labels":{"run":"benchmark-42","variant":"cold-cache"}}}
```

This is an additive, versioned context extension; top-level intent/observation
versions remain1. Both supported API wires use the existing Capture path. Native
CLI accounting remains its existing unsupported measurement scope; no native
provider accounting or billing certification is added.

## Validation, ownership and boundaries

At most16 entries; keys match `[A-Za-z][A-Za-z0-9_.-]{0,63}`; values are nonempty
printable Unicode strings, at most256codepoints and256UTF8bytes each. Canonical
UTF8 JSON for the label map must be at most4096bytes. No nested values, numbers,
booleans, nonfinite values, control characters, duplicate mapping items or custom
serialization hooks. Invalid values raise a generic ValueError without echoing
input, before recorder/provider/CLI effects.

Reserved keys (case-insensitive) are all existing attribution/optional/request
identity field names plus namespace, environment, provider_id, credential_account,
authorization, api_key, api-key, headers, request_headers, prompt, messages, tools,
input and input_data. Use `run` for a diagnostic label and existing `run_id` for
actual trusted run identity. Labels never grant tenant/actor/payer authorization,
change storage namespace, select rates or make accounting billing-eligible. Hosts
must supply safe labels; bounded text cannot detect a secret under arbitrary names.
Nothing is automatically copied out of client/model input, payloads or headers.

Context defaults continue to inherit attribution fields only. Metadata, like the
existing actor/run optional fields, is explicit, not inherited from recorder
or deployment defaults. Mutating original maps after construction or callback
copies must not affect later requests. `to_dict()` returns an owned plain JSON
copy; `child()` retains the exact frozen metadata. Decode accepts the exact legacy
shape or legacy shape plus the extension with exactly version/labels; unknown
version/keys, malformed or empty serialized extensions are refused.

## Resume and identity

Existing Harness0.9.12 delegates context serialization/validation/child creation
to public Runtime functions. Prove installed compatibility rather than modifying
or republishing Harness. Its saved context is authoritative on resume: old saved
labels remain even if a caller supplies different new metadata, just as changing
recorder defaults does not relabel persisted work. To change a run/variant, start
a new run. Test this explicitly and document it; no implicit label migration.

Metadata does not enter artifact digests, prepared-turn input/context identities,
workflow/bundle fingerprints, prompts or provider bodies. Absent/empty metadata
must preserve exact legacy serialized context, intent field set and accounting
fingerprints, including golden source/installed fixtures. A nonempty extension
intentionally changes its accounting payload hash and sealed private checkpoint/
receipt content. Replaying the same call/intent is idempotent; changing labels
under an existing call ID is a conflict. Unknown usage is not retry consent.

## Persistence and migration

The published PostgreSQL recorder0.3.0 validates context through Runtime's public
`ResolvedContext.from_dict` and stores the whole intent as JSONB. New Runtime
validation therefore permits the additive extension without changed adapter code,
columns, privileges, migrations or adapter republishing. Prove this with the
actual published0.3.0 wheel on PostgreSQL16/18. Old records remain byte/hash
identical and are not rewritten. New labelled contexts require Runtime>=0.9.17
for validation/resume; do not downgrade a reader that needs those contexts.
Existing unlabeled records remain readable. Reporting scope/filters/rates and
assessments stay unchanged; raw operator `calls()` may expose labels, so hosts
must retain existing public-view filtering and never treat labels as authority.

If the published adapter fails this contract, reproduce it upstream and make an
explicit adapter compatibility release using newly qualified bytes, never replace
0.3.0. No assumption of migration-free compatibility substitutes for actual gates.

## Delivery and verification

Tentative next free Runtime0.9.17 and Lab0.9.10, verify availability/main before
use. Core0.5.0, Harness0.9.12, recorder0.3.0/store0.4.1 keep existing public bytes.
Use clean isolated branches and frozen checksummed public dependencies. Native
plan, watched regressions, one fresh whole-branch SDK review and one consumer
review; no per-task implementer agents. Saved original regression files stay
unchanged. Release only exact merged/qualified original artifacts and verify
public PyPI/GitHub bytes, clean imports/consumers and all post-merge CI.

Tests cover validation bounds/privacy, deep snapshot/default/child semantics,
legacy canonical goldens, direct run/turn/prepared calls on both wires, multiple
requests and acquisition/rule-repair paths, failure-before-dispatch, observer and
recorder mutation/failure, stored conflict/idempotence and old adapter PG16/18
persistence/reopen. A copyable Lab `accounting-metadata` blueprint uses public APIs
and shows prepare-time labels, frozen Harness resume and filtered output. Add
installed wheel/rebuilt-source tests, required four Linux AMD64/ARM64×PG16/18
cells and reasoning/public-index/distribution gates; no required infra skips.
Preserve old manifests/profiles/freezes, prompts/corpus/scoring/history, update
current pins only after real publication, and document identity/migration/limits
and the remaining Cerebro backlog in READMEs and version-specific release notes.
