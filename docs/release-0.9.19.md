# Runtime 0.9.19 — context-pack execution contracts

Runtime 0.9.19 provides the execution layer for Harness 0.9.13 context packs.
See [profiled context inputs](context-packs.md) for the public API and the
[release page](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.9.19)
for publication status, original artifacts, hashes and qualification records.

## Changes

- Independent model and authoritative validation input views. Private guard
  facts remain available to explicitly authorized guards without entering the
  provider payload. Private access is declared and uses fail-only handling.
- Evidence variants and retained history fit within one bounded context-work
  budget; callbacks cannot silently replace selected or authoritative facts.
- Frozen v3 turns restore without recapturing, recomposing or recounting input.
  Artifact, tool, guard, profile and input bindings are readmitted before use.
- Public capabilities: `guard_input_views_v1`, `frozen_turn_inputs_v1`, and
  `context_input_variants_v1`. Existing sent-effort and accounting behavior stays
  available.

## Compatibility and migration

Pack-free callers preserve existing payloads and v1/v2 identities. Opting into
profiles, evidence variants or independent private facts uses v3; retain readers
that understand those records when settling saved operations. Do not strip new
fields or relabel an old frozen record. Existing guard-version readmission rules
from earlier releases still apply.

This release introduces no recorder schema or recorder publication. Core 0.5.0
and the original published Runtime PostgreSQL recorder 0.3.0 bytes are unchanged.
Adapter archives attached as witnesses do not authorize replacing that version.

The context-pack train is Core 0.5.0, Runtime 0.9.19, Harness 0.9.13, Harness
PostgreSQL 0.4.2, and recorder 0.3.0. Lab 0.9.12 pins the complete public train;
earlier Lab manifests retain their historical SDK selections.

## Release verification

Publication promotes original artifacts from an exact-source successful SDK
qualification run. Required coverage includes source and isolated installed
wheels on Linux x86/ARM and macOS Intel/ARM with Python 3.11–3.13, Linux dependency
floors, and PostgreSQL 16/18 accounting. Public index downloads must match the
qualified hashes. These synthetic checks establish contract correctness, not
live-model answer quality or savings.
