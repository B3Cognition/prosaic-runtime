# Runtime 0.9.15 — fee-format enforcement

Closes the three documented value-guard gaps: `1.20 EUR`, `1.20 euros` and
`120 cents`. Suffix ISO/custom currency markers and euro words use the existing
currency identity rules; cent(s) scales the amount by one hundredth without
guessing a currency. Answer and tool-result text use the same bounded scanner.

Strict sign agreement, explicit `sign=magnitude`, Decimal rounding, malformed
source refusal and money/x-multiple separation remain enforced. Word markers
do not match prefixes of ordinary words, and Markdown list bullets do not
become negative suffix amounts. Both synthetic provider wires exercise the
invented-fee retry and corrected-answer path.
Adjacent markers cannot hide another amount or turn a number on the next line
into its source. Conflicting units and unsupported compound cent/currency text
are refused rather than losing the explicit currency qualification.

The value guard's version advances to `value-in-tool-output-v2`, changing its
binding fingerprint. Hosts must explicitly readmit affected bindings on upgrade;
old receipts never authorize execution under a silently changed guard. No other
guard, permission or untagged execution contract changes.

Core remains published 0.5.0. PostgreSQL recorder 0.3.0 remains an unchanged
compatibility witness and is not republished. Harness and Lab releases/manifests
are outside this Runtime-only fix; historical expectations and release assets
stay immutable. The unchanged Lab fee cases can be checked with `--runxfail`
against an installed fix wheel.

Release requires fresh source and installed-wheel regressions, all native
OS/Python, PostgreSQL 16/18 and dependency-floor qualification cells, exact-head
overall CI SUCCESS, expected-head merge and equal tested/merged trees. Publish
only original qualified merged-source bytes, then verify actual public archive
hashes, upstream Core lineage, isolated imports, fee enforcement and pip check.
Synthetic qualification does not establish live-provider certification.
