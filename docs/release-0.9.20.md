# Runtime 0.9.20 — safe tool events and captured citations

This release adds explicit host-bound contracts for safe progress, typed tool
results and captured evidence. Existing callers keep their old wire values,
identities and record paths when the contracts are absent.

- Independent scalar argument/result display policies and current display grants.
- Typed `ToolResultV1` with private producer declarations and bounded evidence.
- Delivery proof from actual finalized HTTP requests and responses, plus final
  citation syntax, UTF-8 spans, hashes and current permission checks.
- Citation-enabled text is withheld until ordinary guards and citations accept it.
  Authorization failures never spend a model repair allowance.
- Known journal completion survives cancellation, event-sink failure and evidence
  capacity errors. Aggregate evidence bounds fail closed before further exposure.
- Explicit retained host imports; old history IDs alone grant no citation authority.

Core 0.5.0 and PostgreSQL recorder 0.3.0 retain their published bytes and schemas.
Harness 0.9.14 supplies workflow/session adoption and pack authorization adapters.
Native/Unified and external structured execution refuse unsupported automatic
citation combinations before effects. Provenance does not prove semantic entailment.
Parallel calls, MCP and sub-agents remain separate work.

See [the contract](tool-events-citations.md). Qualification archives carry exact
source, original wheel/sdist hashes, installed consumers, platform/source checks,
PostgreSQL 16/18 and dependency-floor evidence. Promotion is manual from a
successful exact-source `Publish SDK` run; all qualification records are checked
again without rebuilding or rerunning the matrix.
