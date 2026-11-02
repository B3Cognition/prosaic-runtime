# Reasoning effort with DeepSeek, Qwen and Nemotron

Requires Prosaic Runtime 0.9.10+. [reasoning.yml](reasoning.yml) uses the
OpenAI-compatible adapter and [canonical Markdown](.prosaic/subagents/).
Prosaic inspects that prose in place; these examples do not install files into
Claude or Codex directories.

| Profile | Model and serving API | Effort levels | Markdown example |
| --- | --- | --- | --- |
| `deepseek` | DeepSeek `deepseek-v4-pro` | `low`, `high`, `max` | [effort: max](.prosaic/subagents/deepseek-reasoner.md) |
| `qwen` | Alibaba Model Studio `qwen3.8-max` | `low`, `medium`, `xhigh` | [effort: xhigh](.prosaic/subagents/qwen-reasoner.md) |
| `nemotron` | NIM 2.0.13 `nvidia/nemotron-3.5-super-vl-120b-a12b` | `low`, `medium`, `high` | [effort: high](.prosaic/subagents/nemotron-reasoner.md) |

The model catalogue supplies these capabilities automatically; none of these
profiles needs `supported_efforts`. Markdown `effort` overrides the profile's
`features.reasoning_effort: low`, sending the chosen value as `reasoning_effort`
in `/chat/completions`. Unsupported levels fail before inference. `ultra` is
reserved for Codex native execution. Provider compatibility aliases are not
treated as distinct effort levels.

Natural-language instructions describe the task and answer format. The YAML
frontmatter sets the native effort control; saying "think harder" in the body
alone does not change that control.

## Run

Install the release in your project environment:

```sh
python -m pip install 'b3-prosaic-runtime==0.9.10'
```

From the repository root, put your provider key in `DEEPSEEK_API_KEY` or
`DASHSCOPE_API_KEY`. Start your Nemotron NIM deployment for the local example.
Use the endpoint for your region/account; the Qwen config uses the still-supported
Singapore domain. Workspace domains use
`https://{WorkspaceId}.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1`.

The following commands send real requests and can incur charges:

```sh
prosaic-runtime subagents/deepseek-reasoner.md --source examples/.prosaic \
  --config examples/reasoning.yml --profile deepseek \
  --arguments 'What time is 3 hours and 40 minutes after 2:45 pm?'

prosaic-runtime subagents/qwen-reasoner.md --source examples/.prosaic \
  --config examples/reasoning.yml --profile qwen \
  --arguments 'What time is 3 hours and 40 minutes after 2:45 pm?'

prosaic-runtime subagents/nemotron-reasoner.md --source examples/.prosaic \
  --config examples/reasoning.yml --profile nemotron \
  --arguments 'What time is 3 hours and 40 minutes after 2:45 pm?'
```

These are single-turn text examples without tools. Their providers enable
reasoning by default. They do not demonstrate reasoning-history replay for
tool calls or continuing conversations. `max_tokens` caps the response, including
reasoning where the provider counts it; it is separate from effort.

Nemotron is a family, so this profile deliberately names the documented NIM
model. The separate TokenProxy `nemotron-3.5-lightning` example does not establish
Lightning's effort capabilities; use controls documented by that serving API.
For this NIM model, `low` and `medium` select efficient reasoning, while `high`
selects full reasoning.

## Verify without credentials

```sh
.venv/bin/python -m pytest tests/test_reasoning_examples.py -q
```

Tests inspect the actual Markdown through Prosaic and execute the documented
CLI shape against a local HTTP fixture. They check wire fields, all supported
levels and unsupported-level rejection. They do not measure live model quality.

Capability sources, checked 2026-11-02:
[DeepSeek models](https://api-docs.deepseek.com/api/list-models/),
[Qwen Chat API](https://www.alibabacloud.com/help/en/model-studio/qwen-api-via-openai-chat-completions),
[Nemotron NIM reasoning](https://docs.nvidia.com/nim/large-language-models/2.0.13/get-started/advanced/get-started-nemotron-3.5-super-vl.html#control-reasoning).
