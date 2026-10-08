# Prosaic Runtime PostgreSQL metering

This package is unreleased. Install it from this source checkout alongside the
Runtime source checkout implementing `prosaic_runtime.accounting` (or a future release
with those contracts). The packaging floor `prosaic-runtime>=0.5.3` alone does not
guarantee accounting support; an older published 0.5.3 package lacks this capability.
Verify `from prosaic_runtime.accounting import AccountingError, RateCard` before use. Runtime accounting off needs no database or psycopg dependency.

```python
from prosaic_runtime.accounting import ExecutionContext, RateCard
from prosaic_runtime_postgres import PostgresRecorder

recorder = PostgresRecorder(
    dsn, namespace="my-deployment", environment="sandbox",
    defaults=ExecutionContext(application_id="app"),
    rate_card=RateCard("synthetic-v1", "test-model", "2", "8", "0.5"),
)
# Explicit provisioning step using database owner credentials:
recorder.initialize()
recorder.check_ready()
# Pass accounting=recorder to Runtime/Harness. Do not put dsn in model input.
print(recorder.report(tenant_id="default", billing_account_id="default"))
```

The synthetic USD rates above are test fixtures, not current provider prices.
`prepare` acknowledges only after a successful PostgreSQL transaction commit.
`observe` atomically commits immutable evidence and its single effective assessment.
Identical canonical replay is a no-op; conflicting content raises `AccountingError`.
A crash after commit but before acknowledgement can safely retry the exact same source
ID and payload. A new network dispatch must use a new call ID. Missing observations
remain unresolved even when execution failed or was cancelled; they are never zero.
Malformed normalized evidence is quarantined and excluded from monetary sums.
Unknown pricing/model/tier yields an unavailable assessment. The rate card saved in
the dispatch intent is used even after recorder configuration changes.

`calls` and `report` always constrain namespace and environment. Optional application,
tenant, billing account, actor, project, request, run and invocation filters only
narrow this scope. `started_from` is inclusive, `started_before` exclusive; both require
offset-aware timestamps. `calls(limit=1000, offset=0, **filters)` returns at most 1,000 rows. Limits must
be integers from 1 through 1,000; offsets must be nonnegative signed-64-bit integers.
Ordering is stable by dispatch timestamp and call ID. Each page uses an independent
snapshot; concurrent inserts can move offset boundaries, so pages are not a historical
export or consistent multi-page manifest. Reports stream every matching row through
a server-side cursor in one repeatable-read transaction and expose its transaction
timestamp; call pagination does not restrict report totals. Currency sums retain eighteen decimal places and are separate;
unknown coverage is represented by counts and `fully_costed=false`, never a final
zero. Caller authorization and tenant/account binding belong to the host application.
A default payer is attribution only. All calls have `billing_eligible=false`.

This package covers platform-paid OpenAI-compatible text model requests, after each
endpoint's usage semantics have been verified. Tools paid independently, native
subscriptions, BYOK, embeddings, audio, image, video and batch are excluded. Hosts
must track configured excluded resources separately; these reports cannot establish
that an entire run containing unsupported paid resources is fully costed.
No customer charge, commercial outbox, exporter, evidence revision, manual adjustment,
reconciliation, receipt import or billing period closure is implemented. Conflicting
final evidence is rejected; preserve it in an operator incident record for a future
audited resolution, rather than overwriting financial records.

## Operator contract

Assign an accounting service owner before production use. Provision distinct namespaces
and database credentials for sandbox/production. Schema creation is explicit and version
1 only; application methods never run DDL. Use an owner/migration role for `initialize`,
a runtime role with SELECT/INSERT on both tables and UPDATE on intent rows (required by
`SELECT FOR UPDATE`), and a reporting role with SELECT only. Do not grant DELETE or
schema mutation to the runtime role. UPDATE privileges are technical lock permission,
not an application amendment API. Enforce append-only policy with role management;
no unaudited edit/delete endpoint is provided. Limit DSN exposure using secret storage.
Configure PostgreSQL durable storage, `fsync=on`, `full_page_writes=on`, and backups.
Connections request `synchronous_commit=on`; replication durability remains an operator
choice. Standalone writes are bounded by connect/statement/lock timeouts (defaults
5 seconds, maximum configured 30 seconds), with no automatic retry of model dispatch.
`check_ready` checks the schema can be read; it does not prove INSERT privileges, disk
capacity or replication health. A failed `prepare` must prevent new paid dispatch.

Monitor database errors, capacity, unavailable assessments, unresolved/quarantined
calls and unsupported coverage. A report showing a complete measurement may still
lack a cost estimate. Do not describe metering as vendor invoice reconciliation.

For lost acknowledgement: replay the exact immutable intent/observation, then query
its call ID. For unknown completion: leave the prepared call unresolved, inspect
provider request evidence, and follow Harness interrupted-invocation policy; do not
reissue a model request because an accounting acknowledgement was lost. For rate or
routing errors: stop affected execution, retain evidence and investigate frozen
configuration; this MVP intentionally cannot silently amend an assessment.

Use PostgreSQL backups/PITR with a deployment retention policy that retains source IDs,
canonical hashes, scope and financial evidence. Stop writers while restoring and verify
restored counts/hashes and the recovery point against provider/Harness evidence before
resuming execution. Calls dispatched after the recovery point may be missing; do not
pretend restoring a backup proves complete coverage. Restore tests should replay known
retained IDs and verify no duplication. No exporter exists here. If one is added later,
keep it stopped until retained vendor delivery state has been reconciled: blindly
replaying restored outbox rows can duplicate charges. Never delete deduplication IDs
just to reclaim disk space. Backup testing, operational ownership, retention and
provider reconciliation remain deployment release requirements.

## Verification

```sh
# Against an owned disposable database; credentials must not be committed.
METERING_TEST_DSN=... PYTHONPATH=src:adapters/postgres/src \
  .venv/bin/python -m pytest adapters/postgres/tests -q
uv build --directory adapters/postgres
```

Without `METERING_TEST_DSN`, database tests skip explicitly; that is not release proof.
