# Releases and compatibility

Runtime 0.9.23 adds [bounded sub-agents](release-0.9.23.md) and pairs with Harness 0.9.17.

Runtime 0.9.22 adds the [optional MCP client](release-0.9.22.md) and pairs with Harness 0.9.16.

Runtime 0.9.21 adds [parallel read tools](release-0.9.21.md) and pairs with Harness 0.9.15, store 0.4.4 and Lab 0.9.14.

Runtime 0.9.20 adds [safe events and captured citations](release-0.9.20.md).
The tools train pairs it with Harness 0.9.14 / store 0.4.3 and Lab 0.9.13;
Core 0.5.0 and recorder 0.3.0 are unchanged. Publication is confirmed by the
versioned release page and its original artifact receipts.

Context-pack release train prepared on 2026-11-09. Runtime 0.9.19 adds
profiled model/private inputs and frozen restoration; Harness 0.9.13 supplies
named agent defaults and workflow-node context selection. Lab 0.9.12 pins the
complete public family. Release pages and their original artifacts are the
publication authority; earlier Lab manifests remain unchanged.

| Component | Context-pack train | Scope |
| --- | --- | --- |
| Core (`b3-prosaic`) | 0.5.0 | Unchanged canonical Markdown and inert rule tags |
| Runtime (`b3-prosaic-runtime`) | 0.9.19 | Profiled inputs, private guard facts, evidence fitting and frozen v3 turns |
| Harness (`b3-prosaic-harness`) | 0.9.13 | Reusable context packs with per-agent/node selection |
| Runtime PostgreSQL recorder | 0.3.0 | Original published bytes and existing schema |
| Harness PostgreSQL store | 0.4.2 | Explicit context-pack storage migration and fenced renewal |
| Integration Lab | 0.9.12 | Public context-pack adoption and complete train qualification |

See [Runtime 0.9.19 notes](release-0.9.19.md) and the
[Lab context-pack guide](https://github.com/B3Cognition/prosaic-integration-lab/blob/v0.9.12/docs/context-packs.md).
Historical Lab 0.9.11 retains Runtime 0.9.18 / Harness 0.9.12 and its sent-effort
blueprint. New SDK availability does not certify a partial historical mixture.

Install current SDKs in a fresh environment; pin versions and archive hashes for reproducible deployments. Optional PostgreSQL adapters require explicit provisioning and operator readiness checks. Never co-install legacy and renamed distributions that own the same imports.

Runtime 0.9.15 advances `value_in_tool_output` to `value-in-tool-output-v2`; explicitly readmit affected bindings when upgrading from 0.9.14. Runtime 0.9.16 headers add no identity fields and leave guard versions unchanged. Harness 0.9.12 adds workflow guard admission, accepted dependency facts and bounded durable proof while preserving untagged identities and Validator V1. See its [identity/readmission contract](https://github.com/B3Cognition/prosaic-harness/blob/v0.9.12/docs/workflow-rules.md) and the [Lab guide](https://github.com/B3Cognition/prosaic-integration-lab/blob/v0.9.9/docs/workflow-enforcement.md).

Runtime 0.9.17 labels add no artifact/workflow identity fields. Empty labels preserve
old accounting hashes; nonempty labels intentionally enter accounting payload hashes
and saved receipts. Existing saved Harness labels win on resume. New labeled
contexts require >=0.9.17 readers; old unlabeled records are unchanged. See the
[accounting migration contract](accounting.md#request-metadata-v1).

## Independently versioned recorder

[PostgreSQL recorder 0.3.0](release-history/postgres-0.3.0.md) is the current
adapter release. Its version and public bytes are unchanged by Runtime 0.9.19;
attached adapter witnesses do not constitute a new adapter publication.

## Published history

Every published version is linked below. Backfilled notes explain changes using that version’s source, without attributing newer features or modern validation results to it. Candidate tags without a published release are not listed as releases.

| Release | Published (UTC) | Notes |
| --- | --- | --- |
| [v0.9.19](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.9.19) | See release page | [Context-pack notes](release-0.9.19.md) |
| [v0.9.18](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.9.18) | See release page | [Release notes](release-0.9.18.md) |
| [v0.9.17](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.9.17) | See release page | [Release notes](release-0.9.17.md) |
| [v0.9.16](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.9.16) | 2026-11-07 | Published notes |
| [v0.9.15](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.9.15) | 2026-11-06 | [backfilled notes](release-history/v0.9.15.md) |
| [v0.9.14](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.9.14) | 2026-11-06 | [backfilled notes](release-history/v0.9.14.md) |
| [v0.9.13](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.9.13) | 2026-11-05 | Published notes |
| [v0.9.12](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.9.12) | 2026-11-02 | Published notes |
| [v0.9.11](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.9.11) | 2026-11-02 | Published notes |
| [v0.9.10](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.9.10) | 2026-11-02 | Published notes |
| [v0.9.9](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.9.9) | 2026-10-29 | [backfilled notes](release-history/v0.9.9.md) |
| [v0.9.8](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.9.8) | 2026-10-24 | Published notes |
| [v0.9.7](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.9.7) | 2026-10-17 | Published notes |
| [v0.9.6](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.9.6) | 2026-10-17 | Published notes |
| [v0.9.5](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.9.5) | 2026-10-15 | [backfilled notes](release-history/v0.9.5.md) |
| [v0.9.4](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.9.4) | 2026-10-14 | [backfilled notes](release-history/v0.9.4.md) |
| [v0.9.3](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.9.3) | 2026-10-14 | [backfilled notes](release-history/v0.9.3.md) |
| [v0.9.2](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.9.2) | 2026-10-13 | [backfilled notes](release-history/v0.9.2.md) |
| [v0.9.1](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.9.1) | 2026-10-13 | [backfilled notes](release-history/v0.9.1.md) |
| [v0.9.0](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.9.0) | 2026-10-13 | [backfilled notes](release-history/v0.9.0.md) |
| [v0.8.1](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.8.1) | 2026-10-11 | [backfilled notes](release-history/v0.8.1.md) |
| [v0.7.1](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.7.1) | 2026-10-10 | Published notes |
| [v0.7.0](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.7.0) | 2026-10-09 | Published notes |
| [v0.6.0](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.6.0) | 2026-10-08 | Published notes |
| [v0.5.3](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.5.3) | 2026-10-07 | Published notes |
| [v0.5.2](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.5.2) | 2026-10-07 | Published notes |
| [v0.5.1](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.5.1) | 2026-10-01 | Published notes |
| [v0.5.0](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.5.0) | 2026-10-01 | Published notes |
| [v0.4.0](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.4.0) | 2026-10-01 | Published notes |
| [v0.3.0](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.3.0) | 2026-09-30 | Published notes |
| [v0.2.0](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.2.0) | 2026-09-29 | Published notes |
| [v0.1.0](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.1.0) | 2026-09-29 | Published notes |

## Release-note checklist

For each future publication, write version-specific notes before release: describe changed behavior, dependency floors and the exact qualified train, identity/fingerprint changes and readmission or migration steps, adapter publication versus witness status, and observed validation with its limits. Link the original qualification and public-consumer proof. Never substitute a generic “qualified release” sentence or rewrite historical package bytes to improve documentation.
