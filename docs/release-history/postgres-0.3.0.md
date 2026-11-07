# Runtime PostgreSQL recorder 0.3.0

The independently versioned `b3-prosaic-runtime-postgres` package selects
`b3-prosaic-runtime>=0.9,<0.10` and Psycopg binary >=3.2.1,<4. It retains
explicit database provisioning, readiness checks, scoped immutable usage
observations and unknown/partial accounting evidence. The Python import remains
`prosaic_runtime_postgres`; ordinary Runtime execution needs no database.

The adapter version is unchanged by Runtime 0.9.14–0.9.16. Archives attached to
those SDK-only releases are qualification witnesses; their presence does not
announce another publication of recorder 0.3.0. Accounting request labels and
per-call sent-effort evidence remain separate roadmap requests, not features
of this adapter release. Native subscription usage is outside its supported
paid-API accounting scope.

See the [adapter setup and limitations](../../adapters/postgres/README.md) and
[release matrix](../releases.md). Database readiness and synthetic PostgreSQL
qualification do not certify a production deployment or a customer invoice.
