# Fee-format regression fix and Runtime release

> Execute natively with the debugging, test-driven-development and verification skills.

**Goal:** Fix the three documented `1.20 EUR`, `1.20 euros` and `120 cents`
misses, add real guard/wire regressions, merge and publish the next Runtime fix.
The maintainer explicitly authorized implementation, merge and release.

**Architecture:** Extend the existing shared answer/source scanner in
`guards/values.py`; keep currency identity, Decimal rounding, sign policy,
malformed-source refusal and separation from x-multiples. Euro words denote EUR;
unqualified cent(s) denote hundredths without inventing a currency code. Advance
the guard version because its binding semantics change. No new dependency or
live inference is needed. Core stays released 0.5.0.

**Spec:** The maintainer's request to fix all three remaining fee-format xfails.

1. Add failing unit and both-wire retry/fail regressions, with supported-fee,
   sign, currency, scale, malformed-source and ordinary-word controls. Run the
   existing guard baseline and watch the new failures before production edits.
2. Implement the smallest shared scanner/matching change, document the bounded
   grammar and readmission impact, then run focused and full source tests.
   Validate a fresh installed wheel and the unchanged Lab fee cases using
   `--runxfail`; never edit historical regression expectations or release pins.
3. Prepare the next unused Runtime version, review the fix, require exact-head
   overall CI SUCCESS, expected-head merge and equal tested/merged trees.
   Qualify exact merged bytes with the existing Core lineage and all native,
   PostgreSQL and floor gates; promote the original qualified artifacts.
4. Verify actual PyPI/GitHub bytes, lineage, isolated imports, regressions and
   pip check. Preserve all old release assets and user worktrees. Report the
   release and any validation limits; no Harness/Lab publication is included.
