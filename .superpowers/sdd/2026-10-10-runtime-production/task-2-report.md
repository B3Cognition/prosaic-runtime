# Runtime Task 2 — R2 finite invocation allowances

Base: `c7c83a1`, branch `codex/ecosystem-production`.
Environment: Python 3.11.15; macOS 27.2 ARM64.
Implementation complete; fresh independent review remains owned by root.

## Behavior and boundaries

Added optional, strictly validated nonnegative integer policy fields
`max_provider_requests`, `max_tool_calls`, and `max_reported_tokens`. `None`
preserves legacy behavior; zero is a real zero allowance. An invocation-owned
`InvocationBudget` shares counters across acquisition and follow-up turns.

Provider reservation occurs at the bounded HTTP entry before accounting intent
preparation or the opener. Failed HTTP and accounting preparation attempts count,
and no retry was added. Tool reservation occurs once at
`BoundedToolRegistry.execute_message`, before name/argument rejection. Denied,
malformed, and each batched dispatch attempt consume one allowance. Repeated
deadline/cancellation/callback boundary checks consume none. Initial-tool mismatch
returns before dispatch and consumes zero tool attempts. Future R3 replay dispatch
will pass through this same boundary; no journal implementation was added here.

Usage is retained before budget admission, after the provider response context
and accounting observation persistence. Native and OpenAI tool turns normalize
once at `_post_chat_turn`; OpenAI text-only terminal Results normalize once in
the bounded Runtime. Strict token caps reject missing, invalid, partial, or
conflicting terminal usage with `usage_unknown`. Over-cap terminal usage fails
with `token_limit`, retaining terminal text, reported totals/details, and known
prior sums. Exactly-at-cap final completion succeeds; further provider/tool
dispatch at equality is prevented. Unknown totals remain `None`; known prior
sums remain diagnostic evidence rather than a promoted complete total.

Capability and opt-in count metadata are `invocation_budgets_v1`; the metadata
contains `provider_requests`, `tool_calls`, `reported_tokens`, and
`usage_complete`. Request/tool exhaustion reasons are `provider_request_limit`
and `tool_call_limit`. Optional telemetry classifies all four as `budget_failure`
with closed reason values. Disabled policies add no count metadata. Result's
seven serialized fields and low-level consumer defaults remain unchanged.

Rulings:

- Endpoint `max_tokens` remains a provider output control. Reported-token caps
  bound continued dispatch and can overshoot by one returned request; they are
  not prepaid invoice estimates or hard callback preemption.
- Harness `max_calls` retains workflow-invocation units. No Harness files or
  cross-workflow accounting were changed.
- OpenAI parsers historically omit unnamed malformed calls and invalid usage
  quantities. Default-false malformed retention and default compatibility usage
  normalization hooks preserve low-level behavior; capped Runtime opts in to
  retaining denied malformed attempts and checking raw token fields before
  omission can make them appear trusted.
- Empty/partial terminal SSE snapshots cannot reuse an earlier complete total;
  conflicting reported snapshots are unknown in strict cap mode. Snapshots are
  not summed as separate requests.

## Approved H1 dependency correction

Root requested a scoped R2 telemetry addition after fresh H1 review reproduced
loss of supported RunStore revisions beyond 128 bytes. Safe string revisions now
accept up to 512 UTF-8 bytes. InvocationScope and label limits remain 128 bytes,
oversized/control/format/non-string revisions remain omitted, and oversized
records drop labels first while retaining the 4096-byte bound. Eight focused
regressions cover the supported and rejected boundaries. This is the only R1/H1
scope addition; no R3 context/journal dispatch was introduced.

## Watched RED / GREEN evidence

All fixture commands below use synthetic owned loopback HTTP/SSE servers, with
no live provider or paid calls. The sandbox denied local binding; credential-free
test escalation was used without weakening production confinement.

1. `.venv/bin/python -m pytest tests/test_invocation_budgets.py -q`
   first returned **18 failed, 51 errors**: absent RunPolicy fields plus sandbox
   loopback-bind errors. This was not accepted as semantic boundary proof.
2. Escalated `.venv/bin/python -m pytest tests/test_invocation_budgets.py -q --tb=short`
   returned **69 failed**, all expected missing-policy-field TypeErrors.
3. After policy validation only, escalated
   `.venv/bin/python -m pytest tests/test_invocation_budgets.py -q --tb=short -k 'allowances_reject or overcap_paid_terminal or token_exhaustion'`
   returned **3 failed, 18 passed, 48 deselected**: real tool callbacks still ran
   after exact/unknown token exhaustion and over-cap terminal response lacked a
   failure reason. These failures preceded budget enforcement.
4. First enforcement run returned **3 failed, 66 passed**: unnamed malformed
   calls had been omitted before dispatch, and a capped text-only success added
   unnecessary reported-usage metadata. Both were corrected. Then
   `.venv/bin/python -m pytest tests/test_invocation_budgets.py tests/test_telemetry.py -q --tb=short`
   returned **110 passed**.
5. Revision regression RED:
   `.venv/bin/python -m pytest tests/test_telemetry.py -q -k 'store_revision' --tb=short`
   returned **2 failed, 6 passed, 33 deselected**, both expected missing revisions.
   The same command after the fix returned **8 passed, 33 deselected**.
6. Expanded compatibility run returned **1 failed, 249 passed** because a newly
   added HTTP-error test expected `failure_reason` instead of the released native
   `provider_error_code`. The test was corrected to preserve the actual contract;
   HTTP production behavior was unchanged.
7. New terminal snapshot RED:
   `.venv/bin/python -m pytest tests/test_invocation_budgets.py -q -k 'terminal_stream_snapshot' --tb=short`
   returned **2 failed, 1 passed, 99 deselected**: an empty terminal snapshot and
   a contradictory smaller total still succeeded. After strict snapshot checks,
   `.venv/bin/python -m pytest tests/test_invocation_budgets.py -q -k 'terminal_stream_snapshot or failed_request_attempts' --tb=short`
   returned **6 passed, 96 deselected**.
8. Focused gate before the final HTTP-usage parity correction:
   `.venv/bin/python -m pytest tests/test_invocation_budgets.py tests/test_usage_integrity.py tests/test_accounting.py tests/test_anthropic_accounting.py tests/test_acquisition.py tests/test_custom_tool_transport.py tests/test_telemetry.py -q --tb=short`
   returned **253 passed** in 92.83s. This includes 102 budget regressions across
   native/OpenAI JSON/SSE, tool/text paths, invalid policies, zeros, batches,
   acquisition, denial, partial usage, accounting failures, cancellation,
   initial-tool mismatch, strict telemetry and default metadata compatibility.
9. Full suite `.venv/bin/python -m pytest -q --tb=short` returned
   **639 passed, 1 skipped** in 210.90s before the final HTTP-usage parity
   correction. This run is superseded by the final full rerun below.
10. Final failed-HTTP parity RED:
    `.venv/bin/python -m pytest tests/test_invocation_budgets.py -q -k failed_http_usage --tb=short`
    returned **1 failed, 1 passed, 102 deselected**. Native already marked usage
    unknown; OpenAI text-only failed HTTP did not. One terminal normalization
    condition was corrected to account an attempted response without usage when
    strict token caps are enabled. The existing provider error code is preserved.
    The same command then returned **2 passed, 102 deselected** in 1.09s.
11. Final full suite `.venv/bin/python -m pytest -q --tb=short` returned
    **641 passed, 1 skipped** in 213.45s. The skip is the unchanged
    `tests/test_cli_sandbox.py::test_framework_python_starts_without_a_broad_prefix_grant`,
    which requires a framework Python build. Root's baseline separately covered
    that operational sandbox lane. This source-suite skip is not new qualification
    evidence for it. No failing tests were omitted. The final suite includes all
    104 budget cases and all 41 telemetry cases.

`git diff --check` passed. No extra agents, pushes, version/package changes,
releases, live calls, or production TypeScript/Node execution were introduced.
