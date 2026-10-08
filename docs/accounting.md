# Customer usage metering

Accounting is explicitly enabled. Existing SDK and CLI calls keep working without
IDs, a recorder, a database or a changed top-level Result schema. Prosaic agent
definitions do not contain customer identities.

## Local source installation

This feature is not published. Install the local Runtime and its separate adapter
alongside the local Harness when testing the integration. Harness pins the corresponding
Runtime implementation commit; Git dependency installation requires that commit to be
available remotely. Opt-in accounting requires `accounting_v1` and refuses an older
adapter, while old unmetered callers remain supported.

```sh
uv pip install --python .venv/bin/python --no-deps -e .
uv pip install --python .venv/bin/python 'psycopg[binary]>=3.2,<4'
uv pip install --python .venv/bin/python --no-deps -e adapters/postgres
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
