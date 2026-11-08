# Sent reasoning-effort evidence

Date: 2026-11-08. Maintainer-selected Cerebro request #4: implement, test, merge
and release Runtime result evidence plus a Lab blueprint and release notes.
Native execution and the existing release process remain selected. No live calls.

## Purpose

Cerebro needs to distinguish a setting that Runtime sent from one that never
reached the transport. A request proves neither provider receipt nor compliance.
Report the actual allowlisted controls at the dispatch boundary, including
defaults, omission, tools, guard repair, failures and safe pool fallback.

This extends the exported ProsaicRuntime run/turn/execute_prepared,
NativeRuntime and UnifiedRuntime result contracts and RuntimeAdapter aggregation.
Low-level raw transport/constrained helpers are internal, not a second public
result contract. Native evidence describes process arguments; inner CLI model
requests and local vendor settings are unobserved. Anthropic effort remains
unsupported; its ordinary requests explicitly show omission.

## Public result contract (builder choices)

Add `Result.metadata['reasoning_effort_v1']`, a fresh owned JSON document:

```json
{
  "version": 1,
  "attempt_count": 1,
  "complete": true,
  "omitted_attempts": 0,
  "attempts": [{
    "index": 1,
    "transport": "http",
    "backend": "openai-compatible",
    "profile": "benchmark",
    "model": "example",
    "provider_call_id": "0123456789abcdef0123456789abcdef",
    "requested_effort": "high",
    "configured_effort": "low",
    "format": "reasoning_effort",
    "sent": {"reasoning_effort": "high"},
    "provider_confirmation": "not_observed"
  }]
}
```

`requested_effort` is validated artifact preference; `configured_effort` is the
normalized operator profile default, before an artifact override. Neither is a
claim about actual transmission. `sent` alone is the allowlisted exact encoded
HTTP control or native argument subset. Binary thinking-on does not become a
claim that a high/medium tier was transmitted.

- HTTP formats: `reasoning_effort`, `thinking_toggle`, `chat_template_effort`,
  `omitted`, or `unavailable`. Fields are only `reasoning_effort` (existing enum)
  and `chat_template_kwargs.enable_thinking` (strict boolean). Omission means
  neither control appeared. The literal `none` and boolean false remain explicit
  controls, distinct from omission/server-default-unknown. Unrecognized control
  shapes yield unavailable/incomplete evidence, never inferred omission.
- Native formats: `claude_cli`, `codex_cli`, or `omitted`. `sent` contains only
  `argv`: `['--effort','high']`, `['-c','model_reasoning_effort="high"']`, or
  `[]`. The subset comes from the actual command passed to the child, with no
  prompt, executable/config path, permissions, environment or arbitrary argv.
- `provider_call_id` matches actual HTTP correlation/accounting IDs; null for
  native processes. Profile/model identifiers are captured only as printable
  strings <=256 UTF8 bytes; otherwise null, without rejecting an existing valid
  execution. No endpoint URLs, prompts, headers, keys or provider reasoning text.
- An HTTP row means an attempted transport handoff after the existing final
  boundary/reservation; connect failure still has a row. It does not prove
  delivery. Native rows require `ProcessOutcome.launched`. Metadata discovery,
  prepare failure, pre-dispatch cancellation, refused admission/capacity skip and
  failed process startup produce no sent row.
- `index` is the one-based order of observed rows. All ordinary rows retain
  chronological order. Aggregation never replaces an earlier attempt with only
  the last profile's settings. Unknown delegate dispatch makes count/omission
  null and complete false; known failures do not automatically authorize retries.
- All results reached through these entry points carry the extension, including
  zero-attempt results. Exceptions that do not produce Result cannot fabricate
  sent evidence. Trusted custom adapter hooks cannot replace captured evidence.

Use at most64 rows and32768 canonical JSON bytes, keeping the first rows and
explicit count/omission/completeness. Do not introduce new invocation/request
caps. When a caller's TurnLimits metadata cap cannot fit all rows, trim only the
extension with explicit omission. If even its minimal envelope cannot fit,
absence means unavailable, never zero requests or confirmed omission. Existing
success, guard, receipt, token, tool, cancellation and accounting semantics win.

## Architecture and identity

A small pure module validates/owns/merges the bounded document. API invocations
own a collector outside the backend so accounting/late failures retain rows.
Read only allowlisted fields from the actual encoded request bytes; attach at
the final existing dispatch boundary after prepare and authority checks, before
opener handoff. Do not change payload bytes, headers, encoding or dispatch count.
Keep requested/default annotations separate from this byte-derived evidence.

Native execution derives the safe argument subset from native_command and
includes it only after successful process launch. Native/unified pools and
RuntimeAdapter aggregate child evidence into owned result documents, preserving
previous attempts across fallback and failure. Turn sanitization validates and
bounds the extension and published Harness receipts retain it as result data.

No fields enter artifact/rule/prepared input/workflow/bundle identities. No
CompletedTurn or TurnEvidence schema change. Saved private result receipts may
contain this additive metadata; old receipts remain valid and are not rewritten.
Do not invent historical evidence on recovery: old receipt absence is unavailable.
Neither published PostgreSQL adapter changes or republishes. Correlation IDs let
applications join result evidence to existing accounting calls; ledger schemas,
intents, rates/reports/authority and prepare metadata remain unchanged.

## Verification and release

Watch regressions fail before code. Independent loopback fixtures compare actual
received body controls and correlation headers, and child fixtures report actual
argv. Cover run/turn/prepared, standard/default/none/omission, automatic/explicit
Gemma and Qwen formats, Anthropic omission/refusal, streaming, tool/acquisition/
guard repair, timeout/HTTP failure, failed prepare/observe, cancellation/limits,
native startup/capacity/fallback, unified and adapter aggregation, concurrency,
returned-copy mutation, bounds and forged/malformed documents. Preserve identity
goldens and original saved regression files; no live provider inference.

Baseline mains Runtime3bdcd79198f770af5a0ef8630064ac9bb8c877f2 and
Laba579039998759d0c8a573779ef42a6531d2ee41b have fresh complete exact-head CI
SUCCESS from the completed preceding release, verified before this change.
Do not duplicate that unchanged baseline. New candidates need fresh complete
source/installed wheel smokes, public Harness compatibility, OS/Python/PG gates,
review, expected-head merge/equal trees and immutable qualified-byte promotion.
Runtime0.9.18 and Lab0.9.11 were checked unused; recheck before tagging.

Lab ships a copyable sent-effort blueprint, installed synthetic wire/CLI tests
and guide; it uses public installed APIs. Advance only active manifests to actual
public Runtime bytes after publication, preserving other4SDK pins, previous
profiles/freezes, corpus/prompts/scoring and all previous blueprint resources.
Require the full ten-job matrix, wheel/rebuilt-source/public-index checks, exact
CI artifact digests/tree checks, anonymous public consumers and independent
post-merge proof. Current READMEs/release notes/Cerebro backlog across all bits
advance through documentation companions without unrelated SDK releases.

No new retry allowance, provider capability discovery, native CLI features,
Anthropic thinking support, raw request logging, billing or Cerebro app edits.
