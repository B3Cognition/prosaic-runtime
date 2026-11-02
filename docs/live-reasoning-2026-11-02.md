# Live reasoning-control verification — 2026-11-02

These observations verify native controls on two user-supplied oMLX servers.
They are not a statistical accuracy or performance benchmark. The change is in
the development checkout after Runtime 0.9.10; the published 0.9.10 package does
not contain the new canonical `none`/Gemma mappings.

[Machine-readable evidence](evidence/live-thinking-2026-11-02.json) contains the
wire settings, prompt hashes/counts, server usage, independently checked answer
values and formatting results. Endpoint addresses and credentials are omitted.
Generated reasoning text is not retained in that report.

## Method

- Models: `ThinkingCap-Qwen3.8-27B-MLX-4bit-DWQ` and
  `gemma-4-31B-it-oQ4e-mtp`. The second endpoint identifies its architecture as
  Gemma 4; no Gemma 4.1 identity was advertised.
- Actual canonical Markdown was inspected by Prosaic and executed through
  `Runtime.run`. A loopback witness proxy recorded request fields and response
  metrics while forwarding to the authorized model servers.
- Updated SDK cases supplied neither `supported_efforts` nor a wire-format
  override. The model catalogue selected the control mapping automatically.
- The question and messages were identical within each model's comparison.
  Temperature was zero, output allowance 4,096 tokens, non-streaming, no tools,
  no structured-output mode and no separate thinking-token budget.
- Each captured request's messages and effective template settings were passed
  to the server's `/v1/tokenize`, then decoded through `/v1/detokenize`. Actual
  generation prompt-token counts matched all ten reconstructed prompts.

The synthetic question asks for the smallest positive n with residues 3 modulo 7,
5 modulo 11, and 7 modulo 13. Independent exhaustive search over 1 through 1001 gives 423.
Each state has one observation here; the released-SDK baseline separately
repeated Qwen's three positive settings. Do not infer a quality ranking from it.

## Qwen: the settings are applied

| Markdown effort | Wire thinking | Native tier field | Prompt tokens | Reasoning tokens | Answer n |
| --- | --- | --- | ---: | ---: | ---: |
| `none` | false | omitted | 60 | 0 | 173 — incorrect |
| `low` | true | low | 88 | 626 | 423 |
| `medium` | true | medium | 58 | 560 | 423 |
| `xhigh` | true | xhigh | 100 | 427 | 654 — incorrect |

All four native-control and prompt checks passed. The positive settings produced
three distinct prompt hashes: low inserts its brief-thinking instruction,
medium uses the template's middle behavior, and xhigh inserts its detailed
reasoning instruction. These match the installed template and server's actual
generation prompt counts. The same three positive counts and answers appeared
in the released Runtime 0.9.10 baseline, whose custom alias capabilities came
from authenticated server metadata.

`none` uses the template's explicit off switch and omits `reasoning_effort`.
This matters because oMLX's effort compatibility fallback can map a literal
`reasoning_effort: none` to a positive alias. The off case generated zero
reasoning tokens.

The two arithmetic misses remain recorded as failures of answer correctness.
The live harness's combined control/correctness assertion therefore failed for
Qwen even though all control checks passed. Higher effort did not produce more
tokens or a better answer on this question. These controls are soft model
instructions, not guaranteed budgets or correctness levels.

## Gemma: one binary switch

| Markdown effort | Native thinking | Prompt tokens | Reasoning tokens | Answer n |
| --- | --- | ---: | ---: | ---: |
| `none` | false | 60 | 0 | 423 |
| `low` | true | 63 | 2491 | 423 |
| `medium` | true | 63 | 2492 | 423 |
| `high` | true | 63 | 2495 | 423 |
| `xhigh` | true | 63 | 2494 | 423 |
| `max` | true | 63 | 2495 | 423 |

All six native-control, prompt and arithmetic checks passed. Every positive
setting produced the same rendered prompt hash and native boolean control.
The small variation in generated token counts does not indicate separate tiers.
The on prompts contained the model's thinking marker; the off prompt did not.

A direct-API baseline showed why a mapping is needed: sending only
`reasoning_effort: high` left native thinking off, with zero reasoning tokens
and the same prompt hash as explicit off. Sending
`chat_template_kwargs: {enable_thinking: true}` enabled the thought channel.

An off switch controls the native thought channel. Gemma still included a
calculation in its final answer with thinking off. It does not guarantee a terse
answer or suppress every explanation.

## Formatting and scope

The prompt asked for JSON, without enabling JSON mode. Many answers used
Markdown fences, and Gemma's off case included explanatory prose. Valid raw JSON
format and correct numeric answers are assessed independently in the evidence;
these observations do not qualify structured-output support.

`ultra` remains Codex-only. Unsupported canonical `none` settings are rejected
before inference; legacy API operator defaults remain compatible separately.
Server-wide settings, templates and model files were not changed.

For reproduction, use the [local Markdown/profile examples](../examples/LOCAL_REASONING.md)
with credentials supplied privately. Request tokenization with the same messages
and `chat_template_kwargs` recorded in the evidence. Compare the returned count
with generation's `usage.prompt_tokens`, and compare prompt hashes within each
model. Do not assert that output token counts must rise with effort.

The live probe used this canonical body and the task above as `arguments`:

```markdown
---
name: probe
description: Live effort verification
execution: agent
effort: low
---
Solve {{args}}. Return only a JSON object with the integer field n.
```

Change only the effort value between calls. Keep the question's terminal period
when reproducing prompt hashes. The generated prompt includes the body's period
after `{{args}}` as well. Formatting compliance is measured without stripping or
rewriting the saved model response; numeric correctness is checked separately.

Native-control references:
[Gemma thinking](https://ai.google.dev/gemma/docs/capabilities/thinking),
[oMLX effort handling](https://github.com/jundot/omlx/blob/main/omlx/reasoning_effort.py),
[ThinkingCap model](https://huggingface.co/bottlecapai/ThinkingCap-Qwen3.8-27B-MLX-4bit-DWQ).
