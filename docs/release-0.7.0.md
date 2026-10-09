# Runtime 0.7.0 release verification

Ships bounded native Anthropic text, streaming and tools with recorder 0.1.1.
OpenAI-compatible execution remains the default. Recorder readiness validates
schema/catalog/types/constraints and actual runtime grants without DDL; stored
accounting format is unchanged. Customer charging remains application-owned.

Local macOS ARM verification: 455 core tests passed, one framework-interpreter
case skipped on this non-framework interpreter. Recorder matrix: 39/39 on
PostgreSQL 16 and 18, zero skips, exact-owned cleanup verified. Receipts:
`59f805a61a83440aa977738826582a54` / `9de1a9363c224d27a963a3989dc04c46`.
Initial owned-lab setup receipts are preserved: two tests needed a CREATEDB
administrator and the Harness test helper import path; no production fix was
needed for those setup errors. Upstream CI excludes only the consumer-owned
Harness resume case and explicitly provisions the limited readiness role.

Core and adapter wheel/sdist builds passed. A clean environment installed both
release wheels using declared dependencies, verified their versions/imports and
Runtime CLI, and resolved the immutable Prosaic 0.3.1 pin. No paid inference.
Independent release review found no blocking version/dependency/test-wiring defect.
Native CI and remote asset verification are recorded in the release train ledger
in the integration lab; local tests do not certify external providers or deployment.
