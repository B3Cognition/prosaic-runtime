# Cerebro invocation headers and release implementation plan

Execute natively using the debugging, TDD, isolated-checkout and verification skills. The maintainer explicitly approved implementation, tests, merge and release.

Goal: Runtime host-supplied per-invocation headers for OpenAI-compatible provider requests, plus durable documentation of all Cerebro follow-ups. Harness separately ships the approved versioned-validator blueprint.

Architecture: add a bounded, privately snapshotted `request_headers` keyword to `ProsaicRuntime.run` and `turn` (including prepared turns). Forward only through the invocation-local HTTP backend. Keep prompts, prepared input/artifact digests, workflow identity, accounting documents and observations unchanged when omitted; never persist raw custom headers. Runtime retains ownership of credentials, framing and correlation headers. Unsupported providers refuse nonempty custom headers before HTTP. No live calls, new dependencies or user-worktree changes.

Files: new `src/prosaic_runtime/request_headers.py`, new `tests/test_request_headers.py`; update `runtime.py`, public docs and current version/qualification assertions. Existing closed request limits, cancellation, accounting and no-redirect behavior remain authoritative.

1. Baseline HTTP/accounting tests; add failing real-loopback tests for run/turn/prepared calls, follow-up requests, concurrent and sequential separation, caller mutation, no disclosure, malformed/reserved headers, unsupported provider refusal and unchanged identity/omitted behavior.
2. Implement validation and invocation-local propagation. Headers are a mapping of ASCII token names to ASCII text values, case-insensitive unique, bounded to32 entries/8192 bytes/2048 bytes per value. Refuse controls, credential/framing/proxy/correlation headers. No interpolation or model-supplied authority.
3. Run focused, fresh complete source and installed wheel gates; review. Prepare next unused Runtime version, require exact-head overall SUCCESS, merge expected head and verify equal trees.
4. Qualify exact merged source using released Core0.5.0 lineage and all native/PG/floor gates. Promote original bytes, verify public PyPI/GitHub hashes, clean consumer tests and source rebuild. Then qualify/release Harness against the published Runtime.

Review focus: headers cannot leak across invocations or into body/receipts; invalid values fail before callbacks/HTTP; case-variant overrides cannot replace runtime-owned headers; continuation/retry requests retain the host snapshot; empty options preserve old identities.
