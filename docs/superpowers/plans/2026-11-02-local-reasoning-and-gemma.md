# Live reasoning verification and Gemma toggle mapping

User authorized live Qwen tests on localhost and Gemma support on a supplied LAN
endpoint. The shared oMLX key is read privately; no credential enters evidence.
The actual Gemma model is `gemma-4-31B-it-oQ4e-mtp` (Gemma 4).

Use one neutral Markdown `effort` field. Add `none` below `low`; Gemma maps `none`
to `chat_template_kwargs.enable_thinking: false` and low/medium/high/xhigh/max to
true. Positive levels share one setting on this binary model. Ultra stays Codex
only. Leave native adapter positive levels and legacy API settings compatible.

For local Qwen hybrid templates, keep the real low/medium/xhigh tiers; a none
setting must disable thinking through its template rather than fall back to low.
Use documented exact model identities, with an optional operator-owned wire
format override for other aliases. Pure admission/preparation perform no I/O.

1. Add failing HTTP/CLI/admission tests for canonical none and toggle/hybrid
   mappings, all API execution paths, defaults, restrictions and model overrides.
2. Implement shared wire projection and exact-model defaults. Validate trusted
   format configuration and preserve existing API/default/native behavior.
3. Run focused and full tests. Verify the new mappings against both live servers
   and record redacted reproducible evidence, separating native prompt controls,
   answer correctness, output formatting and stochastic reasoning length.
4. Update examples/docs and obtain independent code review.

Initial released Runtime 0.9.10 live observations: Qwen wire values low/medium/
xhigh produced matching rendered prompt counts 88/58/100 for identical messages,
and reasoning counts 626/560/427. Low and medium solved the CRT question with
n=423 (low wrapped its JSON in a Markdown fence); xhigh returned the incorrect
n=654. Gemma effort-high-only and explicit off
both rendered59 prompt tokens and zero reasoning tokens; explicit on rendered62
and produced2148 reasoning tokens. These are control checks, not quality rankings.

The updated Runtime's explicit none test also produced a Qwen arithmetic miss
(n=173), with zero reasoning tokens and the off-template prompt verified. Keep
answer correctness separate from native control verification in the evidence.

## Verification

- Final full source suite: 1680 passed, 2 skipped in543.01s.
- Focused effort/conformance regression selection: 276 passed.
- Development wheel built successfully;55 installed-wheel thinking/Markdown
  tests and package ownership/CLI smoke passed.
- Independent review found no remaining blockers after strict canonical-none
  admission, target-model capability resolution and conformance format fixes.
- Updated live SDK: all10 wire/prompt control checks passed. Gemma answered all6
  correctly; Qwen answered2of4 correctly, with the none/xhigh misses retained.
- This is an unreleased follow-up, separate from published Runtime0.9.10.
