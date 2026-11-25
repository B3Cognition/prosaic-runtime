# Runtime 0.9.30

Runtime reuses Core's rule parsing and tag stripping when a conversation prepares
or readmits a turn with the same agent prose. Each of the two in-process caches
holds up to 128 entries. Callers still receive independent copies of parsed
rules and the same model-facing prose. Existing rule bindings and saved-turn
identities are preserved. The upgrade requires no configuration, readmission,
or storage migration.

The regression tests check reuse, cache bounds, isolation between callers, and parity
with Core's parsing and stripping. The release workflow exercises the source,
installed wheels, and PostgreSQL integration across its existing qualification
matrix. The receipt attached to the [versioned release](https://github.com/B3Cognition/prosaic-runtime/releases/tag/v0.9.30)
identifies the exact source and artifact digests.

Qualification uses Core 0.5.0 and the published Harness 0.9.12 wheel for consumer
tests. The PostgreSQL recorder remains independently published at 0.3.0. The
attached recorder artifacts serve as qualification witnesses.

See [PR #51](https://github.com/B3Cognition/prosaic-runtime/pull/51) for the
implementation and regression tests.
