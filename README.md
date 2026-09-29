# Prosaic Runtime

Execute neutral Prosaic commands and agents on OpenAI-compatible Chat Completions endpoints. A Python library and CLI for small, bounded tasks with streaming, tool calls, explicit filesystem permissions, and structured results.

## First run: from installation to an answer

Follow these steps in order in one terminal. Commands below target macOS, Linux,
or Windows through WSL, using Bash or Zsh. You need an existing OpenAI-compatible
Chat Completions endpoint: this runtime does **not** install or host a model.
Local and hosted endpoints both work; hosted calls may incur provider charges.

### 1. Check prerequisites

Install Git, Python 3.11 or newer (with pip and venv), and Node.js 20 or newer
(with npm), then check:

```sh
git --version
python3 --version
node --version
npm --version
```

Use a Python executable that reports 3.11+ throughout. Prosaic itself is a Node.js
CLI used to inspect neutral prose; the runtime executes that prose in Python.

### 2. Install

Start in a directory where you keep projects. This creates a fresh checkout and
an isolated Python environment; no sudo or global Python installation is needed:

```sh
git clone https://github.com/B3Cognition/prosaic-runtime.git
cd prosaic-runtime
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

Install the tested Prosaic revision inside this checkout, and link its CLI into
the same virtual environment:

```sh
mkdir -p .tools
git clone https://github.com/B3Cognition/prosaic.git .tools/prosaic
cd .tools/prosaic
git checkout 0f7e187
npm ci
npm install --global --prefix "$VIRTUAL_ENV" "$PWD"
cd ../..
```

Here `--global` uses the explicit virtual-environment prefix, not the system
installation directory. Keep `.tools/prosaic` in place: the installed CLI links
to that checkout.

Confirm both commands are available:

```sh
prosaic --help
prosaic-runtime --help
```

For future terminals, enter the checkout and run `source .venv/bin/activate`
again. The Python package installs PyYAML automatically.

### 3. Open the ready-made examples

```sh
cd examples
```

Stay in this directory for the remaining commands. The checkout already contains:

```text
examples/
  .prosaic/subagents/summarizer.md   # prose without tools
  .prosaic/subagents/reviewer.md     # prose requesting read tools
  evidence/pilot.md                 # sample input for the reviewer
  prosaic-runtime.yaml              # no-tool endpoint configuration
  with-tools.yml                   # read-tool endpoint configuration
```

There are no files to create before the first run. The neutral Markdown prose
uses YAML frontmatter; runtime configuration uses YAML too.

### 4. Configure your endpoint and credentials

Open `prosaic-runtime.yaml` and `with-tools.yml` in your editor. In **both** files,
replace `base_url` and `model` with values supplied by your endpoint operator.
Keep the different `allowed_tools` values: `[]` for the first example and
`[read_file]` for the second.

The no-tool file looks like this:

```yaml
default_profile: small
allowed_tools: []
limits:
  timeout_s: 180
  max_tool_rounds: 8

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

The localhost URL and `your-model` are placeholders. Start your local model server
separately, or use your hosted endpoint's URL. Supply the API base (often ending
in `/v1`), **not** the full `/chat/completions` URL: the runtime appends that path.
Use the endpoint's exact model identifier. The tool example additionally needs
function-calling support.

If your endpoint requires a Bearer API key, enter it without putting it in shell
history or a configuration file:

```sh
export LOCAL_LLM_API_KEY="$(python -c 'import getpass; print(getpass.getpass("Endpoint API key: "))')"
```

For an unauthenticated local endpoint, leave that variable unset:

```sh
unset LOCAL_LLM_API_KEY
```

Run only the credential command appropriate for your endpoint. Do not commit
credentials. If the endpoint rejects usage streaming, set `stream_options: false`;
if it does not stream, set `streaming: false`. If it rejects `reasoning_effort`,
remove `effort: low` from `.prosaic/subagents/summarizer.md`.

Check setup before making a model request:

```sh
prosaic-runtime doctor --output text
```

This checks configuration, the Prosaic CLI, configured credentials and authenticated
model discovery. It does **not** run inference. A server without a `/models` route
produces a warning; authentication and connectivity failures produce a failed check.
Model discovery alone does not prove completion or tool support.

### 5. Run prose without tools

```sh
prosaic-runtime subagents/summarizer.md \
  --arguments 'S1: The pilot processed 120 requests. S2: Three requests timed out. S3: The cause has not been established.' \
  > summary.json
```

Check the result and print the answer:

```sh
python -c 'import json; r=json.load(open("summary.json")); print(r["stdout"]); print("exit_code:", r["exit_code"]); assert r["exit_code"] == 0, r["stderr"]'
```

An illustrative answer is:

```text
The pilot processed 120 requests (S1). Three requests timed out (S2).
The cause has not been established (S3).
exit_code: 0
```

Wording varies by model. `summary.json` contains a result object with
`event: "result"`, `exit_code`, `stdout`, `stderr`, usage, and metadata. No tool is offered
to this agent. Execution success does not establish factual correctness—compare
the answer against the supplied input.

### 6. Run prose with a read-only tool

```sh
prosaic-runtime subagents/reviewer.md \
  --config with-tools.yml \
  --arguments 'Review evidence/pilot.md. What do we know, and what remains unknown?' \
  --allow-tool read_file \
  --read-root ./evidence \
  > review.json
python -c 'import json; r=json.load(open("review.json")); print(r["stdout"]); print("exit_code:", r["exit_code"]); assert r["exit_code"] == 0, r["stderr"]'
```

Expect a read of `evidence/pilot.md`, followed by **Observed**, **Unknown**, and
**Next check** sections. Findings should cite that path and source IDs: 120 requests,
three timeouts, correctness not evaluated, timeout cause unknown, and no comparison
against another endpoint. A suggested next check is collecting server logs, not
claiming a diagnosis.

The tool must be requested by prose, allowed in configuration, **and** granted by
the invocation. Reads are limited to `evidence/`; this example cannot write files,
execute shell commands, or spawn agents.

### 7. Watch streaming events

```sh
prosaic-runtime subagents/reviewer.md \
  --config with-tools.yml \
  --arguments 'Review evidence/pilot.md.' \
  --allow-tool read_file \
  --read-root ./evidence \
  --events
```

This prints one JSON object per line: events followed by a final `result` object.
It is JSONL, not a single JSON document. Look for `text_delta` events when the
endpoint streams text, tool progress, and the final result's `exit_code: 0`.
Intermediate events are diagnostic; the final result is authoritative.

Without `--events`, status and a five-second elapsed-time heartbeat go to **stderr**,
including while the model is reasoning silently. They do not contaminate JSON or
text on stdout. Use `--quiet` to suppress progress; errors remain visible.

### 8. Run the repeatable live smoke test

```sh
prosaic-runtime smoke --live --config with-tools.yml
```

`--live` is required: this deliberately makes model requests and may incur charges.
The installed package includes the same neutral summarizer/reviewer prose and sample
evidence, so this works without the repository's examples directory when you supply
a configuration path. It uses `default_profile`, or the profile selected with
`--profile NAME`, for both probes.

The test creates a temporary directory with synthetic evidence and grants only
`read_file` within that directory for the reviewer. It never reads your project
files. This fixed test grant is independent of your configuration's `allowed_tools`;
`--live` opts into these two narrowly scoped probes, not general agent permissions.
Timeout and round limits still apply to each probe separately.

The JSON report separates `completion`, observed `streaming`, and `tool_execution`
for each example. Exit 0 requires both completions and an actual successful file read;
a model merely claiming it read the file is not enough. Streaming is reported separately
and is not required for success, because non-streaming endpoints are supported.
This is a capability smoke test, not a model-quality benchmark.

For a single no-tool inference check instead:

```sh
prosaic-runtime doctor --inference --timeout 180
```

Inference is never a fallback for a failed default doctor check: only the explicit
`--inference` flag enables that request.

### Troubleshooting

| Symptom | Check or fix |
| --- | --- |
| `python3`, `node`, or `npm` missing | Install the prerequisites from step 1; on some Linux systems, Python venv is a separate OS package. |
| npm reports audit or deprecation warnings | These refer to Prosaic's pinned dependency tree. Review `npm audit` in `.tools/prosaic`; do not blindly apply forced dependency upgrades. Warnings alone do not prove installation failed—check both CLI help commands. |
| `prosaic-runtime` not found | Activate `.venv` from the checkout root and rerun `python -m pip install .`. |
| `prosaic` not found | Activate `.venv`, enter `.tools/prosaic`, and rerun `npm install --global --prefix "$VIRTUAL_ENV" "$PWD"`. |
| No configuration or artifact found | Run from `examples/`; otherwise pass `--config` and `--source` explicitly. |
| YAML error | Use spaces for indentation and preserve the nested structure shown above. |
| Connection refused or timeout | Start the local endpoint or check its host, port, connectivity, and availability. |
| HTTP 401/403 | Check the API key, its permissions, and `api_key_env`; do not print the key in logs. |
| HTTP 404 or model error | Check the API base path and exact model ID; do not include `/chat/completions` in `base_url`. |
| Unsupported request field | Disable `stream_options` or `streaming`, or remove prose `effort`, as described in step 4. |
| Tool unavailable or read denied | Check all three grants and `--read-root`; use a function-calling model and a path inside `evidence/`. |
| Nonzero exit or incomplete output | Read the result's `stderr` and `metadata`; check token/round limits before retrying. Setup errors are printed to terminal stderr instead. |

The runtime exits nonzero on failure. Files such as `summary.json` can be empty
when configuration fails before execution; inspect the terminal error first.
There are no automatic retries or paid-model quality guarantees.

## Configuration and further examples

Configuration discovery checks `prosaic-runtime.yaml`, then `prosaic-runtime.yml`,
in the current directory. Use `--config path/to/config.yml` or pass a path to
`ProsaicRuntime.from_config()` to select another file. Only YAML/YML is supported.

See the [examples walkthrough](examples/README.md) for the neutral prose files,
permission details, and Python equivalents. To create your own agent, add a
Markdown file under `.prosaic/subagents/` with neutral YAML frontmatter and a body
using `{{args}}`, following the checked-in summarizer or reviewer.

## Plain-text output

For just the answer in the terminal, use `--output text`:

```sh
prosaic-runtime subagents/summarizer.md --arguments 'Text to summarize' --output text
```

JSON remains the default. `--events` emits JSONL and cannot be combined with
`--output`. On failures, text mode prints any partial answer to stdout, a diagnostic
to stderr, and exits nonzero. Do not treat a partial answer as a successful result.

## Python

The complete runnable program is [examples/run_examples.py](examples/run_examples.py).
From the checkout root, run `python examples/run_examples.py` after configuring the
example YAML files and setting your API-key environment variable. See its
[usage instructions](examples/README.md#runnable-python-sample) for endpoint/model
overrides and streaming events.

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

YAML can override time and round defaults:

```yaml
limits:
  timeout_s: 180
  max_tool_rounds: 4
```

CLI `--timeout` and `--max-tool-rounds` override these values independently.
The Python API uses YAML limits when `policy` is omitted; an explicit `RunPolicy`
replaces them. The checked-in examples use 180 seconds to accommodate slower models.

`on_event` receives `started`, `text_delta`, `progress`, `text`, `tool_started`,
`tool_completed`, and `completed` dictionaries. Tool events contain `name`, `call_id`,
and `turn`; completion also contains `status` and `duration_ms`. They include denied
calls and omit arguments and file contents. The older diagnostic `progress` events
can contain paths or model text, so do not treat the entire event stream as redacted.
The final `Result` is authoritative. Event handlers execute synchronously; keep them
fast. The `cancelled` callback is checked between streaming reads, tool-loop operations,
and requests. In-flight blocking I/O is bounded by the HTTP timeout; cancellation is cooperative.

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
