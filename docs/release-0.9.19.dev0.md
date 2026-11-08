# Runtime 0.9.19.dev0 context-pack candidate

Unpublished development candidate. See [profiled context inputs](context-packs.md)
and the paired Harness qualification report. This train uses existing Core0.5.0
and original recorder0.3.0 bytes; neither needs republication.

The three new public capabilities support Harness context packs. Ordinary
`run`/`prepare_turn` callers retain default payloads and identities. Opting into
profiles, variants or private facts uses the versioned v3 contract. Private
guards require audited access declarations and fail-only handling.

Promotion requires the exact built bytes, paired Harness/adapter acceptance,
and all hosted release gates, including native Intel macOS. Local emulation
does not substitute for that required native cell. No paid models were run.
