# Local Qwen and Gemma thinking controls

Requires the development checkout after Runtime 0.9.11. The tagged 0.9.11
release does not include these canonical `none`/Gemma mappings. Install this
checkout in your project environment before using [local-thinking.yml](local-thinking.yml).

The same Markdown `effort` field controls both models:

| Effort | ThinkingCap Qwen 3.8 | Gemma 4 |
| --- | --- | --- |
| `none` | Thinking off | Thinking off |
| `low` | Low tier, thinking on | Thinking on |
| `medium` | Medium tier, thinking on | Thinking on |
| `high` | Unsupported | Thinking on |
| `xhigh` | Extra-high tier, thinking on | Thinking on |
| `max` | Unsupported | Thinking on |
| `ultra` | Codex-only, rejected here | Codex-only, rejected here |

Gemma's positive values share one binary setting. `low` always requests some
native thinking; `none` requests its off control. An off control does not stop
the model from explaining a calculation in its final answer. Omitting effort
leaves the provider default unchanged.

The catalogue recognizes the exact served model IDs in the example, official
Gemma 4 instruction-tuned IDs, and their documented community aliases. It does
not guess support for unknown future model families. Unknown/custom aliases can
use the operator's `features.reasoning_effort_format` override described in the
[effort reference](../docs/native-execution.md#reasoning-effort-from-prose).

## Run

Set `LOCAL_LLM_API_KEY` privately in your environment if required. Update the
profile URLs for your servers; Gemma may run on a separate LAN host. No key is
stored in YAML. From the Runtime repository root:

```sh
prosaic-runtime subagents/thinking-solver.md --source examples/.prosaic \
  --config examples/local-thinking.yml --profile gemma \
  --arguments 'What is 17 times 23?'

prosaic-runtime subagents/direct-solver.md --source examples/.prosaic \
  --config examples/local-thinking.yml --profile gemma \
  --arguments 'What is 17 times 23?'
```

Use `--profile qwen` for the same two Markdown files on Qwen. These commands make
real inference requests. Source inspection does not copy Markdown to native
vendor directories or change server-wide model settings.

Gemma requests carry only `chat_template_kwargs.enable_thinking`. Local Qwen
positive requests also carry their `reasoning_effort`; `none` omits that field
so oMLX cannot remap it to a positive alias. This avoids a successful HTTP
response masking a reasoning parameter that the template ignored.

The live evidence report records rendered prompt counts and reasoning tokens;
it does not assume higher effort must generate more tokens on each question.
See [live control verification](../docs/live-reasoning-2026-11-02.md).

Native-control references:
[Google Gemma thinking](https://ai.google.dev/gemma/docs/capabilities/thinking),
[oMLX effort compatibility](https://github.com/jundot/omlx/blob/main/omlx/reasoning_effort.py),
[ThinkingCap Qwen model](https://huggingface.co/bottlecapai/ThinkingCap-Qwen3.8-27B-MLX-4bit-DWQ).
