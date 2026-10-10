# Public package channel

After the qualified first publication, install in a fresh Python 3.11+ environment:

```sh
python -m venv .venv
.venv/bin/python -m pip install 'b3-prosaic-runtime>=0.8,<0.9'
.venv/bin/python -m pip install 'b3-prosaic-runtime-postgres>=0.2,<0.3'
.venv/bin/prosaic-runtime --help
```

The imports remain `prosaic_runtime` and `prosaic_runtime_postgres`; CLI commands
remain unchanged. Runtime's normal index dependency is `b3-prosaic>=0.4,<0.5`.
PyPI's unrelated `prosaic` distribution is never a migration shortcut. The legacy
GitHub distributions and new B3 distributions share module paths; create a new
virtual environment or image rather than installing both families together.
Historical wheels, manifests and receipts retain their original names.

Availability requires successful publication and verification of downloaded
public bytes. Candidate wheels and CI artifacts alone do not establish it.

## Trusted publishing

The five final PyPI publisher identities use owner `B3Cognition`, workflow
filename `publish.yml`, and environment `pypi`:

| PyPI project | GitHub repository |
| --- | --- |
| `b3-prosaic` | `prosaic` |
| `b3-prosaic-runtime` | `prosaic-runtime` |
| `b3-prosaic-runtime-postgres` | `prosaic-runtime` |
| `b3-prosaic-harness` | `prosaic-harness` |
| `b3-prosaic-harness-postgres` | `prosaic-harness` |

PyPI permits only one pending project for the same GitHub
owner/repository/workflow/environment tuple. Bootstrap the family in stages:

1. Register `b3-prosaic`, `b3-prosaic-runtime` and `b3-prosaic-harness` as the
   initial pending entries for their three repositories.
2. Promote Core first. For each paired SDK release, its first successful OIDC
   exchange creates the main project and converts its pending publisher. That
   first paired attempt may publish matching main-package files and then fail
   because the adapter is not yet authorized.
3. Once that conversion is visible in the owning account, register the adapter's
   pending entry with the same tuple and repeat the guarded promotion using the
   original successful qualification run. The next exchange adds the adapter to
   the existing publisher; digest checks allow matching partial uploads to finish.

Retrying alone cannot register the adapter. GitHub release waits for the complete
verified pair. After bootstrap, one normal publisher can authorize both projects.
See PyPI's [pending uniqueness and publisher reuse implementation](https://github.com/pypi/warehouse/blob/f43a0f79dbaf40e4888110ad5b1d5e0ad87121b6/warehouse/oidc/models/github.py#L373-L416)
and [token exchange and scope](https://github.com/pypi/warehouse/blob/f43a0f79dbaf40e4888110ad5b1d5e0ad87121b6/warehouse/oidc/views.py#L208-L368).

An authorized PyPI owner registers the tuples following the
[official guide](https://docs.pypi.org/trusted-publishers/using-a-publisher/).
The repository's `pypi` environment restricts deployment to intended version tags.
OIDC permission belongs only to the publication job; GitHub release write access
belongs only to its separate job. No long-lived PyPI token is needed.

## Exact-byte qualification and promotion

`Publish SDK` archives one exact source commit, builds the Runtime and recorder
wheel/sdist pairs once, and audits committed source, exact name/version/dependency
metadata, SPDX license declarations and payloads, entry points and wheel RECORD
hashes. Untracked files and working-tree changes cannot enter the source archive.

Its build receipt (`release-receipt.json`, version1) records source identity and
all four artifact hashes. Separate `qualification-*.json` files record successful
commands, interpreter/platform identities and JUnit counts where applicable.
Every qualification binds the exact package hashes and upstream receipt digest.
A build, fingerprint or fixture report does not itself prove qualification.

While Core publication is pending, supply its explicit successful `Publish SDK`
run ID and exact source commit. The pipeline downloads that run's
`qualified-release`, verifies the Core receipt and hashes, and installs only its
named B3 wheel from a curated wheelhouse. `upstream-receipt.json` retains that
binding. Runtime's required SDK matrix uses those exact Core bytes; Harness must
subsequently select the same Core binding when consuming the Runtime candidate.
Promotion refuses to replace a previously qualified upstream binding.

Cross-repository artifact download requires read access to the selected public
repository's Actions artifacts. The workflow uses its job token when access is
available; an optional `ECOSYSTEM_ARTIFACT_TOKEN` may supply narrowly scoped read
access. Missing access fails qualification. Do not infer access from configuration
or paste credentials into reports or chat.

Required source and installed-wheel checks cover Linux AMD64/ARM64 and macOS on
Python3.11/3.12/3.13. Linux requires the patched Bubblewrap broker and operational
confinement; macOS additionally runs its actual framework-interpreter check.
The Linux source selector excludes only that macOS-specific case. The separately
mandatory clean installed metadata test is excluded from source selectors and
must pass in the isolated wheel environment. Neither exclusion qualifies a
missing security or installed-package gate.

The PostgreSQL recorder additionally requires actual PostgreSQL16/18 on both
Linux architectures, separate migration/runtime roles, installed package imports,
and zero skipped SQL cases. Its upstream lane excludes only the existing
downstream Harness-resume case; the consumer train supplies that integration.
Python3.11 floor checks run on both Linux architectures with PyYAML6.0, pyuca1.2,
markdown-it-py3.0.0, jsonschema4.23.0, referencing0.28.4 and Psycopg3.2.1.
Psycopg3.2.0's binary-extra metadata references an unavailable development wheel.
Harness's separate PostgreSQL store additionally requires libpq17 and a higher
binary-package floor; Runtime's recorder has no such libpq17 restriction.

Tags build and qualify candidates without public uploads. Complete the final
ecosystem code and native service/legacy-upgrade gates before publication.
Publish Core first, Runtime and its recorder second, Harness and its store third,
and then adopt the downloaded SDK bytes in the lab.

Tag the qualified source `v0.8.0` and explicitly dispatch `Publish SDK` on that tag
with `candidate_run_id` naming its successful exact-source candidate, the same
Core run/commit inputs, and `publish=true`. Promotion downloads original qualified
bytes and reruns required checks; it never rebuilds the publication payload.
The stable tag must match the source version, and publisher registration must
actually exist.

Before upload, verify every already present index file by downloading it. A
matching partial upload can resume its missing files. The post-upload check
requires the complete expected file set with matching downloaded hashes.
Different same-version bytes require a new version. GitHub receives the same
qualified distributions and evidence, with no asset overwrite. The lab's active
manifest changes only after verified public downloads and a fresh installed
qualification against those bytes.

Synthetic tool, journal, conformance and service checks do not certify an untested
live provider or deployment's capacity, identity, ingress or HA/PITR behavior.
