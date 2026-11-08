# Customer usage metering

Accounting is explicitly enabled. Existing SDK and CLI calls keep working without
IDs, a recorder, a database or a changed top-level Result schema. Prosaic agent
definitions do not contain customer identities.

## Installation

`b3-prosaic-runtime` 0.9.17 includes `accounting_v1` and
`accounting_request_metadata_v1`; the separately installed
`b3-prosaic-runtime-postgres` recorder stays at its published 0.3.0 bytes.
Install into a fresh environment/image; legacy distributions share import paths.
Older unmetered callers remain supported. After publication use:

```sh
python -m pip install 'b3-prosaic-runtime==0.9.17' 'b3-prosaic-runtime-postgres==0.3.0'
```

## Configure a recorder in trusted host code

```python
import os
from prosaic_runtime import ExecutionContext, ProsaicRuntime, RateCard
from prosaic_runtime_postgres import PostgresRecorder

recorder = PostgresRecorder(
    os.environ['ACCOUNTING_DATABASE_URL'],
    namespace='my-deployment', environment='sandbox',
    defaults=ExecutionContext(application_id='studio'),
    rate_card=RateCard('synthetic-v1', 'actual-model', '2', '8', '0.5'),
)
# An operator initializes tables using a separate migration/owner connection.
# Runtime never creates tables automatically.
recorder.check_ready()
runtime = ProsaicRuntime.from_config('runtime.yml', accounting=recorder)
result = runtime.run('subagents/report.md', 'Generate a report',
    context=ExecutionContext(tenant_id='tenant-42', billing_account_id='account-42'))
report = recorder.report(tenant_id='tenant-42', billing_account_id='account-42')
```

Rates in this example are synthetic USD per million token fixtures, not current
provider prices. Input and output rates are required decimal strings; cache rates
are optional. A cache discount requires known cached-token detail. If no separate
cache rate is configured, all input is assessed at the same explicitly supplied
rate. Requested aliases alone are not evidence of the actual model: a cost estimate
requires the response model to match the rate card. Non-default service tiers and
unsupported modality/prediction usage do not receive a text-price estimate.

Each outbound model request has a unique persisted intent and observation. Records
contain trusted attribution, start/end timestamps, requested/reported model,
provider response ID and token quantities, including cache/reasoning when reported.
They never contain prompts, model content, tool arguments, keys or raw headers.

If context is omitted, configured defaults are applied and then the reserved
`default` IDs. IDs are scoped by deployment namespace and environment; individual
call IDs are always unique. Partial IDs preserve provenance and cannot inherit an
unrelated application/tenant's default payer. Authentication and authorization
belong to the host; an ID grants no authority. This implementation never produces
customer charges: all assessments have `billing_eligible=false`.

`metadata.accounting_v1` identifies resolved context, scope and call IDs. The
legacy `cost_usd=0` remains an unavailable compatibility field, not the calculated
cost. Query the recorder for decimal estimates and coverage. An unknown measurement
is not zero; missing final usage remains unresolved or partial. Conflicting or
duplicate-key provider evidence is quarantined. Repeated identical streaming usage
snapshots are not summed; standard null usage chunks are ignored.

## Request metadata V1

Trusted host code may supply small diagnostic labels through the existing context:

```python
context = ExecutionContext(
    tenant_id='tenant-42', billing_account_id='account-42', run_id='benchmark-42',
    request_metadata={'run': 'benchmark-42', 'variant': 'cold-cache'},
)
# Use context=context with run(), turn(), a prepared turn, or Harness(context=...).
# In a custom recorder's prepare(intent), before HTTP dispatch:
# labels = intent['context'].get('request_metadata', {}).get('labels', {})
```

`prepare(intent)` already receives requested model, profile, artifact and trusted
run/step attribution. V1 adds labels, not the raw request. Nonempty labels serialize
as `context.request_metadata = {'version': 1, 'labels': {...}}`; top-level
intent/observation versions stay 1. Both supported API wires and each accounted
acquisition/tool/repair request use the same frozen labels. Native CLI accounting
keeps its existing unsupported scope; no new CLI label flags are introduced.

Constructors snapshot the mapping immediately. Public `request_metadata` is a
read-only map; `ResolvedContext.to_dict()` returns a fresh plain JSON copy. Child
contexts retain labels. Recorder/deployment defaults inherit attribution fields
only, never metadata. None/empty maps omit the extension entirely.

At most 16 labels are accepted. Keys match `[A-Za-z][A-Za-z0-9_.-]{0,63}`; values
are nonempty printable Unicode, at most 256 characters and 256 UTF-8 bytes each.
The map's sorted compact canonical JSON, including escaped Unicode, is at most
4096 bytes. Invalid input raises a fixed ValueError before recorder or provider
effects. Nested values, non-string types, duplicate mapping items and custom
string hooks are refused. Persisted extensions require exactly version 1 and a
nonempty labels map; unknown versions/keys are refused.

Keys are case-insensitively reserved for existing context identity fields
(`application_id`, `tenant_id`, `billing_account_id`, `actor_id`, `project_id`,
`request_id`, `run_id`, `invocation_id`, `parent_invocation_id`, `step_id`), recorder
scope (`namespace`, `environment`, `provider_id`, `credential_account`) and raw
request/credentials (`authorization`, `api_key`, `api-key`, `headers`,
`request_headers`, `prompt`, `messages`, `tools`, `input`, `input_data`). Use `run`
for a diagnostic label, `run_id` for actual trusted identity. Hosts must avoid
putting secrets under arbitrary label names; text validation cannot detect them.

Labels never authorize tenants/actors/payers, choose storage scope/rates, grant
retry consent or make estimates billing-eligible. They are not copied from client
or model input, and do not reach provider payloads/headers or prompts. Operator
`calls()` and private checkpoints/receipts contain labels; filter public views as
before. `report()` filters and assessments are unchanged and do not use labels.

**Identity and resume:** Artifact digests, rule fingerprints, prepared turn and
workflow/bundle identities gain no fields. Absent/empty metadata preserves legacy
context/intent serialization and accounting hashes exactly. Present labels
intentionally enter the accounting payload hash and sealed private checkpoints/
receipts: identical intent replay is idempotent; changed labels under the same
call ID conflict. Existing Harness 0.9.12 treats saved accounting context as
authoritative, even if a new caller supplies different observational labels or
recorder defaults change. Start a new run to use a new variant.

**Migration:** The published PostgreSQL recorder 0.3.0 delegates context decoding
to Runtime and stores opaque JSONB; no adapter republish, DDL/role changes or old
row rewriting is required. Labeled records and saved runs require Runtime >=0.9.17
when read/resumed; older readers are not promised to understand the new extension.
Legacy unlabeled records remain readable. Deploy the new reader before producing
labeled runs. Never infer no-call safety from an unresolved accounting intent.

## CLI

Optional flags include `--application-id`, `--tenant-id`, `--billing-account-id`,
`--actor-id`, `--project-id`, `--request-id`, `--run-id`, `--invocation-id` and
`--parent-invocation-id`. Without accounting they attach context metadata only.

To enable durable accounting, install the PostgreSQL adapter and provide
`--accounting-dsn-env ACCOUNTING_DATABASE_URL --accounting-namespace my-deployment
--accounting-environment sandbox`. An optional `--accounting-rate-card rates.json`
loads the RateCard fields from a trusted JSON file. Keep the DSN in the environment,
not an argument or prompt. Missing schema/connectivity prevents model dispatch.

## Failure and recovery

Persisted intent is acknowledged before network dispatch. Observation persistence
runs independently of cancellation, and each tool-loop model turn is recorded
separately. A recorder failure returns `failure_reason=accounting_failed`, stops
further dispatch in that invocation and never retries the model. Already committed
usage remains in the ledger even when output validation subsequently fails.

An intent without observation after a crash remains unresolved. It is not proof
that the request failed to execute. Never automatically reissue such a request.
Identical record replay is safe; conflicting final content is rejected. The MVP
does not expose an evidence-revision or manual-resolution API. Operator incident
handling preserves unresolved evidence until supported reconciliation is available.

## Supported scope

Platform-paid OpenAI-compatible text calls are supported after the host verifies
that endpoint's usage semantics and rates. Model calls in acquisition and tool
loops are included. Paid tools, native CLI subscriptions, BYOK/mixed payer costs,
multimodal APIs, FX, real-time money caps, billing exporters and invoice reconciliation
are outside this release. Reports cannot prove whole-run cost coverage for resources
the recorder does not instrument. No production/customer charging release is implied
by the tests: deployment ownership, database durability/retention and health monitoring
remain explicit operational requirements.

See [PostgreSQL installation, privileges and recovery](../adapters/postgres/README.md)
and the companion Harness `docs/accounting.md` for frozen attribution across resume.
