# Runtime 0.9.14 — opt-in rule admission

Publishes the rule-admission implementation reviewed in PR #16, including the
long/empty ID, built-in tool evidence, feedback fault, malformed value source and
signed multiple corrections. Hosts register guards and bind enforcement stages;
untagged artifacts retain their existing behavior. The value guard uses strict
sign agreement by default; `sign=magnitude` explicitly retains magnitude-only
matching. See [the rule admission guide](rules-admission.md).

Core remains the published 0.5.0 release. PostgreSQL recorder 0.3.0 is an unchanged
compatibility witness and is not republished. Guarded Harness conversations
require the companion Harness release; the previously published Harness 0.9.8
does not preserve these policies, registrations or receipts.

Qualification must bind the exact Core release, run source and installed-wheel
checks on Linux AMD64/ARM64 and macOS across Python 3.11–3.13, both PostgreSQL
majors and dependency floors, and execute the tagged-agent wheel gate. Promote
the original qualified artifacts on the version tag, then verify actual public
index bytes and clean installed behavior. Preserve prior release assets and the
unchanged independent review expectations; three old sign-insensitive assertions
are a documented policy mismatch, not passing tests.
