# Runtime production acceptance

The target installed family is `b3-prosaic` 0.4.0,
`b3-prosaic-runtime` 0.8.0 and `b3-prosaic-runtime-postgres` 0.2.0. Python imports
and CLI names remain `prosaic`, `prosaic_runtime`, `prosaic_runtime_postgres`,
`prosaic` and `prosaic-runtime`. Use a fresh environment: old and renamed
distributions own the same import paths. Never install them together.

Runtime requires `b3-prosaic>=0.4,<0.5`; the recorder requires
`b3-prosaic-runtime>=0.8,<0.9` and `psycopg[binary]>=3.2.1,<4`. The 3.2.0 binary
extra names an unavailable development wheel. SDK requirements use ordinary
index version ranges, without direct Git URLs.

## Mandatory installed gate

Install the three explicitly selected qualified wheel files and ordinary
dependencies in a fresh virtual environment. Run from an unpacked source archive
of the matching Runtime commit, using that environment's Python:

```sh
python -I scripts/wheel_smoke.py
python -I scripts/production_wheel_smoke.py
PROSAIC_RUNTIME_WHEEL_ACCEPTANCE=1 python -I -m pytest -o pythonpath= tests/test_package_compatibility.py -q
```

The final command must pass with zero skips. `-I` and the empty pytest
`pythonpath` prevent source imports from substituting for installed code. The
metadata gate checks versions, module ownership, package origins, supported
dependency ranges, public exports and absence of the legacy distributions.
The source suite deliberately skips this installed-only test; that skip cannot
satisfy this gate.

The production smoke uses public SDK APIs, with PATH empty during native
execution. Its only HTTP servers are disposable loopback fixtures. Two isolated
workers reconstruct a contextual tool, invocation scope and a test-only SQLite
journal. Each worker sends exactly two provider requests and reports fourteen
tokens; handler counts are respectively one and zero. A business-domain key
retains one effect across replay. The parent exercises the old one-argument
handler (two requests), a zero provider allowance (zero requests), and a terminal
token-cap failure (one request, seven retained tokens). Observer exceptions
cannot erase these results or enable billing. Workers have hard subprocess
timeouts, Runtime invocations have finite deadlines and fixture HTTP reads have
socket timeouts. Arbitrary trusted callbacks remain cooperatively bounded.

The script explicitly loads `tests/support/tool_journal.py` as a fixture and
reports its origin separately from installed package origins. This SQLite store
is neither a shipped production journal implementation nor a usage ledger.
Offline conformance performs no dispatch, reports unexecuted cases, and leaves
qualification `not_qualified`. Fixture evidence is labeled `fixture`.

## Compatibility witnesses

`tests/test_custom_tools.py::test_legacy_descriptor_golden` pins the historical
descriptor with no contextual fields. `tests/test_accounting.py` pins Result's
seven serialized fields, disabled metadata, the accounting attribution-source
format, and normalized usage quantities. Observation correlation is independent
of accounting attribution. Supplying `InvocationScope` and a failing observer
alone adds no accounting metadata. Explicit accounting remains opt-in; supplying
the historical `ExecutionContext` continues to enable its historical metadata.
Existing low-level provider defaults and ledger formats are unchanged.

The recorder's real PostgreSQL witnesses preserve scoped counts, cached-input
usage, exact decimal estimate `0.003000000000000000`, privacy and duplicate
observation behavior with a failing observer and operation scope. New cases
retain a durable 1,200-token paid fixture observation after a token-cap failure
and after a journal claim failure. A failed claim causes no effect dispatch and
no provider retry. The journal and accounting ledger remain independent.

```sh
PYTHONPATH=adapters/postgres/src:tests python -m pytest adapters/postgres/tests -q -k 'not harness_resume'
```

This command requires an explicitly owned disposable `METERING_TEST_DSN`; the
complete adapter lane also uses its scoped `METERING_RUNTIME_DSN`. Missing DSNs
and database-test skips are not release evidence. Real PostgreSQL 16 and 18 on
native AMD64 and ARM64, Python 3.11–3.13 native source/installed lanes, and the
separate operational sandbox gate remain mandatory in release CI. Do not weaken
Linux confinement or replace a real database lane with an in-memory recorder.

## Local candidate evidence and limits

Local R5 acceptance used CPython 3.11.15 on macOS ARM64 with Core's original
qualified 0.4.0 wheel (commit `aad4bf45ae1c242f4451700ca59b27280010573a`, SHA-256
`120a956fd009ac7db7319a6a2493079441533818320d8adfdcba7b94b47be602`). Runtime
source began at `583bf91988021375abe98980b345bf29433e3b0f`.
The direct dependency floors were PyYAML 6.0, pyuca 1.2, markdown-it-py 3.0.0,
jsonschema 4.23.0, referencing 0.28.4 and Psycopg/binary 3.2.1. Installed metadata
passed with zero skips; the isolated production smoke confirmed the exact
request/effect counts above. Local source-suite and exact final archive receipts
are recorded in the task evidence ledger alongside source bindings and hashes.

No owned PostgreSQL DSN was available locally; five recorder capture tests were
collected, with the downstream Harness test deselected, without claiming database
qualification. No paid provider, shared database, gateway or deployment was
contacted. Local results do not qualify live models, production capacity,
identity, HA/PITR, or the remaining native architecture matrix. Public index
download/hash verification remains a separate gate after publication.
