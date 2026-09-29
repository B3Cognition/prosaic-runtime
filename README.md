# Prosaic Runtime

Execute neutral Prosaic commands and agents on OpenAI-compatible Chat Completions endpoints. A Python library and CLI for small, bounded tasks with streaming, tool calls, explicit filesystem permissions, and structured results.

Requires Python 3.11+. Loading artifacts from disk also requires [B3Cognition/prosaic](https://github.com/B3Cognition/prosaic) and Node.js 20+. The Python package uses PyYAML for configuration. This project uses Prosaic's `inspect` JSON contract; it does not parse an alternative prompt format.

## Install

```sh
python -m pip install 'prosaic-runtime @ git+https://github.com/B3Cognition/prosaic-runtime.git@main'
git clone https://github.com/B3Cognition/prosaic.git
cd prosaic
git checkout b6c97013880bd6517d1e5a67b43ed985719af9f1
npm ci
npm link
cd ..
```

## Run an agent

For complete, runnable examples with and without file tools, start with the
[examples walkthrough](examples/README.md). It includes neutral prose, endpoint
configuration, sample evidence, CLI commands and Python usage.

Create `.prosaic/subagents/summarizer.md`:

```markdown
---
name: summarizer
description: Summarize supplied text
execution: agent
model_tier: fast
effort: low
---
Summarize {{args}} in three sentences. Preserve uncertainty and source identifiers.
```

Create `prosaic-runtime.yaml`:

```yaml
default_profile: small
allowed_tools: []

routes:
  fast: small

profiles:
  small:
    base_url: http://127.0.0.1:8000/v1
    model: your-model
    api_key_env: LOCAL_LLM_API_KEY
    max_tokens: 2048
    features:
      streaming: true
      stream_options: true
```

Configuration uses YAML, like Prosaic. The CLI and `ProsaicRuntime.from_config()`
look for `prosaic-runtime.yaml`, then `prosaic-runtime.yml`, in the current
directory. Use `--config path/to/config.yml` or pass a path to `from_config()`
to select another file. Only `.yaml` and `.yml` files are supported.

```sh
prosaic-runtime subagents/summarizer.md --arguments 'Text to summarize'
prosaic-runtime subagents/summarizer.md --arguments 'Text to summarize' --events
```

The first command emits one JSON result. `--events` emits JSONL events followed by a result. Progress never contaminates the result text. Configuration and invocation errors go to stderr; unsuccessful execution exits nonzero.

For endpoints that do not accept usage streaming, set `stream_options: false`. Set `streaming: false` for ordinary JSON responses. `effort` maps to `reasoning_effort`; omit it when the endpoint does not support it. Compatibility is with the Chat Completions protocol, not every OpenAI API or every optional provider feature.

## Python

```python
from prosaic_runtime import ProsaicRuntime, RunPolicy

runtime = ProsaicRuntime.from_config("prosaic-runtime.yaml")
result = runtime.run(
    "subagents/summarizer.md",
    arguments="Text to summarize",
    policy=RunPolicy(timeout_s=60),
)
if result.exit_code:
    raise RuntimeError(result.stderr)
print(result.stdout)
```

An orchestrator can pass `ProsaicArtifact.from_inspection(inspect_json)` to avoid spawning Prosaic on every invocation. The snapshot includes artifact identity, neutral frontmatter, body, and bundled resources. Results identify the artifact by a SHA-256 digest. Bundle contents are appended as named context; external prose references are the caller's responsibility.

## Tools and authority

Tools are enabled only by the intersection of the artifact's declaration, configuration `allowed_tools`, and invocation `RunPolicy.allowed_tools`. Filesystem tools also require explicit roots or exact output paths. Missing and empty grants deny access. Prose and provider responses cannot expand the grant.

Supported declarations are omitted/empty/`none`, `read`, `write`, or a list of supported tool names in an inspection snapshot. `write` requests both read and write tools. `full` is rejected because this runtime does not implement arbitrary shell execution or delegation.

Read tools: `read_file`, `read_many_files`, `sha256_file`, `list_files`, `list_tree_with_sizes`, `grep_files`, `grep_context`. Write tools: `write_file`, `edit_file`.

For a read agent, set `tools: read` in its Prosaic frontmatter, `allowed_tools: [read_file]` in configuration, and invoke:

```sh
prosaic-runtime subagents/reviewer.md --allow-tool read_file --read-root ./evidence
```

Paths are relative to `--cwd` (the current directory by default) and must resolve inside it. `--write-path` grants one exact path; `--forbid-root` excludes a path and its descendants. An edit grant allows reading the target as part of replacing its contents. Resolved symlink escapes are rejected. These guards are for the provided file tools; they are not an OS sandbox against concurrent hostile filesystem changes.

The CLI provides no shell, web browsing, recursive agents, workflow scheduler, or application state writer. Hosts retain their output validation, workflow decisions and publication rules.

## Limits and events

Default invocation limits: 120 seconds, 8 tool rounds, 256 KiB input. Endpoint defaults: 4,096 output tokens per turn and 8 MiB captured response per turn. Conversation requests are checked against the input limit before tool-loop turns; oversized responses fail. Old tool results can be compacted. Repeated identical tool rounds disable tools for a final answer.

`on_event` receives `started`, `text_delta`, `progress`, `text`, and `completed` dictionaries. The final `Result` is authoritative; progress and intermediate model text are diagnostic. Event handlers execute synchronously. Keep them fast. The `cancelled` callback is checked between streaming reads, tool-loop operations, and requests. In-flight blocking I/O is bounded by the HTTP timeout; cancellation is cooperative.

Usage is reported when supplied by the endpoint. Cost estimation is unavailable in v0.1; `cost_usd = 0` is a legacy compatibility field, not a claim that inference was free. Check `metadata.cost_status`. No retries or model escalation occur automatically. An incomplete or truncated final response is unsuccessful.

## Echelon integration

The shared transport, stream parser, tool loop, compaction and filtering originated in Echelon. Echelon consumes the runtime through a compatibility adapter, retaining its own prompt policy, RE tools, `echelon_result` handling, journals, and delivery capabilities. The low-level `openai_compatible` module is an integration seam for that adapter, not the public agent-definition API. General consumers should use `ProsaicRuntime`.

## Development

```sh
python -m pip install -e '.[test]'
pytest
```

Tests cover extracted transport edge cases, tool denial, path containment, artifact loading, routing, cancellation, and local HTTP/SSE integration. The end-to-end Prosaic test requires the CLI on PATH; CI installs the pinned Prosaic revision. Tests do not require paid LLM access. Successful conformance tests do not establish model quality; evaluate each agent/model pair against its own acceptance criteria.

Licensed under the [Apache License, Version 2.0](LICENSE). See [NOTICE.md](NOTICE.md)
for extraction provenance and [LICENSE-MIT](LICENSE-MIT) for the retained notice
covering historically MIT-licensed code. Third-party components retain their own licenses.
