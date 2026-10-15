# Runtime 0.9.5 candidate

Adds host-generated HTTP correlation and cooperative synchronous execution lifecycle.
See [execution-lifecycle](execution-lifecycle.md) for semantics, host shutdown ordering
and limitations. Correlation is transport-neutral and supports OpenAI-compatible
and native Anthropic endpoints; accounting intent precedes dispatch. Trace IDs do
not imply idempotency, provider receipt or known usage.

No wire accounting schema, intent/observation identity or interrupted-call retry
policy changes. Hosts opt into shutdown draining by fencing/waiting on the public
lifecycle; run/turn and RuntimeAdapter execution tracking is automatic.

Qualification uses synthetic local providers only. Local full run: 1,170 passed,
two skipped and one CLI startup-marker failure within its 750ms tool timeout.
All 12 unchanged CLI boundary cases passed on isolated rerun; both reports are
retained. Wheels/sdists built and clean installed-package/lifecycle qualification
passed. Release remains pending normal review, CI confirmation of the timing-sensitive
lane and explicit publication. No immutable compatibility goldens were relaxed.

## Controlled tool failure recovery

`ToolFailure("read_unavailable")` lets a trusted callback return a proven
completed failure. Runtime commits the bounded error to its existing effect
journal before observing a deadline or cancellation. Replays return that error
without invoking the callback again. Ordinary exceptions and lost journal
acknowledgements remain uncertain. Never use a controlled failure for an
operation whose effect is unknown.
