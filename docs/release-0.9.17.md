# Runtime 0.9.17 — accounting request metadata V1

Trusted hosts can set `ExecutionContext(request_metadata={'run': 'benchmark-42',
'variant': 'cold-cache'})`. A recorder's `prepare(intent)` receives these labels
before each provider dispatch in `intent['context']['request_metadata']['labels']`.
This works with run, turn/prepared turns, acquisition, tool continuations and
rule repair on both supported API wires. No raw request projection or model/client
label extraction is added. Native CLI accounting keeps its existing limitations.

The constructor owns a read-only snapshot. At most 16 string labels are accepted;
keys are bounded ASCII names, values printable Unicode of at most 256 characters/
256 UTF-8 bytes, and canonical escaped JSON at most 4096 bytes. Reserved identity,
scope, request and credential keys are refused. Invalid values fail before effects
with a sanitized ValueError. Labels are diagnostic evidence, never authorization,
rate selection, retry consent or billing eligibility; hosts must not label secrets.
See the complete [validation and accounting contract](accounting.md#request-metadata-v1).

**Compatibility and migration:** The optional extension is exactly
`{'version': 1, 'labels': {...}}`; top-level intent/observation versions remain 1.
Published PostgreSQL recorder 0.3.0 uses Runtime's public decoder and existing JSONB
schema, with no republish, column/role changes or rewritten old records. Core
remains 0.5.0 and Harness 0.9.12 remains compatible. Deploy Runtime >=0.9.17 readers
before producing labeled runs; downgrade readers of labeled contexts are not
promised. Legacy unlabeled persisted contexts remain accepted unchanged.

**Identity impact:** None/empty labels are omitted entirely, preserving exact
legacy context/intent bytes and accounting hashes. Artifact/rule, prepared-turn,
workflow/bundle identities gain no fields. Nonempty labels intentionally change
accounting payload hashes and sealed private checkpoint/receipt content. Replaying
the same intent is idempotent; changing labels under an existing call ID conflicts.
Harness saved context wins on resume even when new observational labels/defaults
are supplied; use a new run for a new variant. Operator calls/private recovery
views contain labels; public filtering remains the host's responsibility.

Qualification requires final source/isolated installed-wheel tests, both synthetic
provider wires, actual published recorder compatibility on PostgreSQL 16/18,
Linux AMD64/ARM64, macOS, dependency floors and exact Core lineage. Publication
promotes only original qualified merged-source Runtime bytes, never rebuilt local
artifacts or unchanged adapter witnesses. Verify public PyPI/GitHub digests and
fresh consumers. Synthetic tests are not live-provider or financial certification.
Lab's blueprint and complete-train qualification follow actual SDK publication;
a newer Runtime alone does not qualify a partial Lab dependency mixture.

Remaining [Cerebro requests](cerebro-requests.md): actual sent effort evidence,
separate bounded rule-repair allowance, comprehensive versioned identity policy,
and automated release-note enforcement. Workflow enforcement and per-call headers
remain supported with their existing budgets, ownership and identity contracts.

## Release provenance and consumer blueprint

The version tag/source is `eb0735e7cc2e70fdcff2fccd0bc80b3ec2850f0b`, with
[successful immutable qualification](https://github.com/B3Cognition/prosaic-runtime/actions/runs/42460821009)
and [publication run](https://github.com/B3Cognition/prosaic-runtime/actions/runs/42463779682).
The [release page](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.9.17)
is the authority for publication status and original artifact receipts.
[Lab0.9.10](https://github.com/B3Cognition/prosaic-integration-lab/releases/tag/v0.9.10)
provides the complete-train [accounting blueprint](https://github.com/B3Cognition/prosaic-integration-lab/blob/v0.9.10/docs/accounting-request-metadata.md)
and public-consumer proof. This current documentation companion does not alter
Runtime0.9.17 package bytes, source receipts, tags or original release assets.
