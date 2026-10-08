# Prosaic Runtime

Version **0.6.0** adds optional customer usage metering and the separate
`prosaic-runtime-postgres` **0.1.0** recorder. Release assets include both packages;
install the Runtime wheel before the adapter wheel. Accounting stays opt-in.

Optional customer usage metering is described in [Accounting](docs/accounting.md).
Existing callers need no IDs or database; durable tracking uses a separately installed
PostgreSQL recorder and does not enable customer charges.

Version 0.5.3 installs Python Prosaic 0.3.1 automatically at an immutable Git
revision. Installation and CI no longer require Node.js or npm.

Version 0.5.3 retains trusted macOS framework Python startup probes using their
declared sandbox working directory and pins the updated permission documentation.

See the [prompt-injection audit and remaining isolation gaps](docs/security.md)
before granting tools access to sensitive workspaces.

Version 0.5.0 adds [custom CLI tools, discovery and offline preflight](docs/cli-tools.md),
with a complete runnable CLI and an optional Understanding adapter.

Version 0.5.0 also adds [host-registered custom tools](examples/README.md#host-registered-custom-tools):
validated Python callbacks with native function calling. Both APIs are opt-in;
Python callbacks execute trusted host code and are not sandboxed. CLI subprocesses
are also unsandboxed by default; the opt-in below isolates them.

## Tool permissions and CLI isolation

Runtime **0.5.2** includes `cli_sandbox_v1` and opt-in CLI isolation.
Versions 0.5.1 and earlier do not include it; upgrading Prosaic alone does not
enable isolation. Start with the
[sandboxed CLI walkthrough](examples/README.md#sandboxed-cli-tools-runtime-052)
and its [complete configuration](examples/cli-tools-sandboxed.yml).

Markdown requests capabilities; it does not grant host authority:

| Layer | Example | Who owns it? |
| --- | --- | --- |
| Agent Markdown | `tools: [analyze_spec]` | Agent author declares what is needed |
| Runtime YAML | `allowed_tools: [analyze_spec]` | Operator allows the tool |
| Invocation | `--allow-tool analyze_spec --read-root evidence` | Host grants this call's tool and readable inputs |
| CLI OS boundary | `cli_sandbox: {mode: required}` in Runtime YAML | Operator requires isolation; prose cannot disable it |

All three tool declarations/grants must intersect. Merely installing a CLI or
adding it to frontmatter grants nothing. For custom CLIs, the operator also
reviews the executable and explicitly trusts its YAML manifest directory via
`tool_directories`; never automatically trust model-generated manifests.

Required mode confines CLI tools and their version probes to read-only evidence,
trusted dependencies and private writable HOME/scratch. Host IP network access
(including loopback) is blocked; model inference/auth stays in the parent.
Linux requires `/usr/bin/bwrap` >= 0.12.0 and working user namespaces; macOS
requires `/usr/bin/sandbox-exec`. Unavailable enforcement fails closed before
inference. There is no automatic unrestricted fallback.

This setting does **not** isolate builtin file tools, trusted Python callbacks,
validators, or native Codex/Claude providers. Builtin file tools still enforce
their own path grants. CLI workspace writes and external-service access are not
supported in required mode, even if builtin write paths are granted. Do not
grant HOME, `/`, or directories containing host Unix sockets as dependency/input
roots. See [the full setup and limitations](docs/cli-tools.md).

## Overview

Version 0.3.0 harness support: `tool_completed` events for successful `read_file`
now include `read_receipts` with path, SHA-256 of the bytes actually read, offset,
lines_read and line_count. Events contain no file text or raw tool arguments.
`RunPolicy(initial_tool="read_file", ...)` requests that granted function on the
first turn only; prose, runtime and host grants must all allow it. This cannot
elevate permissions or guarantee an endpoint honors tool choice. Consumers must
still validate the observed events. Defaults remain unchanged.

Execute neutral Prosaic commands and agents on OpenAI-compatible Chat Completions endpoints. A Python library and CLI for small, bounded tasks with streaming, tool calls, explicit filesystem permissions, and structured results.

Version 0.4.0 hardening: explicit `initial_tool` selections now reject omitted,
substituted or additional first-turn tool calls before executing any of them.
Failure is `tool_choice_not_honored`, with reported token usage retained and no
automatic retry. This does not prove successful tool execution or correct tool
arguments; consumers must still validate receipts. Stream deltas are provisional
until the final result succeeds. Version 0.4.0 also adds opt-in acquisition prose
and the bounded execution fixes described below.

## First run: from installation to an answer

Follow these steps in order in one terminal. Commands below target macOS, Linux,
or Windows through WSL, using Bash or Zsh. You need an existing OpenAI-compatible
Chat Completions endpoint: this runtime does **not** install or host a model.
Local and hosted endpoints both work; hosted calls may incur provider charges.

### 1. Check prerequisites

Install Git and Python 3.11 or newer (with pip and venv), then check:

```sh
git --version
python3 --version
```

Use a Python executable that reports 3.11+ throughout. Prosaic 0.3.0 and the
runtime are Python packages; Node.js and npm are not required.

### 2. Install

Start in a directory where you keep projects. This creates a fresh checkout and
an isolated Python environment; no sudo or global Python installation is needed:

```sh
git clone https://github.com/B3Cognition/prosaic-runtime.git
cd prosaic-runtime
git checkout v0.5.3
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

The package automatically installs Python Prosaic v0.3.1 at immutable commit
`ee145ef29a62dbcf7f5e8abdf1d979749398718d` into the same environment. No global
installation or retained TypeScript checkout is needed. Older Runtime release
tags retain their historical installation docs.

Confirm both commands are available:

```sh
prosaic --help
prosaic-runtime --help
```

For future terminals, enter the checkout and run `source .venv/bin/activate`
again. The Python package installs Prosaic, PyYAML and jsonschema automatically.

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

For a larger demonstration, see the [four-tier launch dossier example](examples/README.md#four-tier-launch-dossier-demo):
four neutral Markdown subagents, a synthetic 5,100-word dossier, YAML model-tier
routing, scoped read-only tools, and a runnable program with text or JSONL output.
Its documented live results distinguish successful execution from answer quality.

For a single no-tool inference check instead:

```sh
prosaic-runtime doctor --inference --timeout 180
```

Inference is never a fallback for a failed default doctor check: only the explicit
`--inference` flag enables that request.

### Troubleshooting

| Symptom | Check or fix |
| --- | --- |
| `python3` missing | Install the prerequisites from step 1; on some Linux systems, Python venv is a separate OS package. |
| `prosaic-runtime` not found | Activate `.venv` from the checkout root and rerun `python -m pip install .`. |
| `prosaic` not found | Activate `.venv` and repeat the pinned Python Prosaic installation in step 2. |
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

### Opt-in acquisition (v0.4.0+)

Pass `acquisition="subagents/acquire-pilot.md"` to `runtime.run()` to send a
short neutral Prosaic artifact first. The final artifact body, resources and
arguments are withheld until the explicitly selected native tool succeeds.
Both artifacts use one model, conversation, deadline and tool-round budget.
The acquisition declaration cannot broaden final prose/host/config grants;
it must declare `policy.initial_tool`. Its tier/effort must match the final
artifact or be omitted. Acquisition receives **no final arguments**; put the
needed path in its own prose. First-turn JSON response formatting is withheld
too, then restored for analysis. Subsequent tool selection is automatic.

A missing/substituted/extra first call blocks with `tool_choice_not_honored`;
a failed required tool blocks with `acquisition_failed`. Neither failure retries
nor switches to preloading. `acquisition_v1` advertises support. Existing calls
without this option retain their behavior. Acquisition requires Runtime v0.4.0+.

Run the complete staged and no-tool examples from this checkout:

```sh
python examples/run_acquisition.py --config examples/tokenproxy.yml --profile qwen
```

Set `TOKENPROXY_KEY` first. Use `--mode staged` or `--mode preloaded` to choose
explicitly, `--no-stream` for non-streaming, or another configured profile.
The preloaded mode reads a fixed example file on the host and supplies its text
to tool-free Markdown prose; it does not fabricate native tool receipts.
See [examples/README.md](examples/README.md#staged-acquisition-and-explicit-preloading).
This program demonstrates execution, not factual validation; the Harness
companion supplies schema and source/quote admission checks.

Tools are enabled only by the intersection of the artifact's declaration, configuration `allowed_tools`, and invocation `RunPolicy.allowed_tools`. Filesystem tools also require explicit roots or exact output paths. Missing and empty grants deny access. Prose and provider responses cannot expand the grant.

Supported declarations are omitted/empty/`none`, `read`, `write`, or a list of supported tool names in an inspection snapshot. `write` requests both read and write tools. `full` is rejected because this runtime does not implement arbitrary shell execution or delegation.

Read tools: `read_file`, `read_many_files`, `sha256_file`, `list_files`, `list_tree_with_sizes`, `grep_files`, `grep_context`. Write tools: `write_file`, `edit_file`.

For a read agent, set `tools: read` in its Prosaic frontmatter, `allowed_tools: [read_file]` in configuration, and invoke:

```sh
prosaic-runtime subagents/reviewer.md --allow-tool read_file --read-root ./evidence
```

Paths are relative to `--cwd` (the current directory by default) and must resolve inside it. `--write-path` grants one exact path; `--forbid-root` excludes a path and its descendants. An edit grant allows reading the target as part of replacing its contents. Resolved symlink escapes are rejected. These guards are for the provided file tools; they are not an OS sandbox against concurrent hostile filesystem changes.

The CLI provides no general-purpose shell, web browsing, recursive agents,
workflow scheduler, or application state writer. Explicitly trusted CLI manifests
can execute fixed application commands. They are unsandboxed in the default
`off` mode; `required` mode adds the CLI OS boundary described above.
Hosts retain their output validation, workflow decisions and publication rules.

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
Staged invocations additionally emit `acquisition_completed` after the required
tool succeeds. If a later turn is cancelled, times out or exceeds an input/
response limit, already reported completed-turn usage is retained and marked
`usage_scope: reported_completed_turns`; it is not an estimate of an unfinished
request's usage.

Usage is reported when supplied by the endpoint. Cost estimation is unavailable in v0.2; `cost_usd = 0` is a legacy compatibility field, not a claim that inference was free. Check `metadata.cost_status`. No retries or model escalation occur automatically. An incomplete or truncated final response is unsuccessful.

## Echelon integration

The shared transport, stream parser, tool loop, compaction and filtering originated in Echelon. Echelon consumes the runtime through a compatibility adapter, retaining its own prompt policy, RE tools, `echelon_result` handling, journals, and delivery capabilities. The low-level `openai_compatible` module is an integration seam for that adapter, not the public agent-definition API. General consumers should use `ProsaicRuntime`.

## Development

Version 0.4.0 hardening: HTTP inference rejects redirects to protect endpoint
credentials. No-tool and tool-loop requests both obey the serialized byte bound.
Prosaic inspection timeouts return a failed `Result`. Public usage totals remain
unknown (`None`) when any completed turn lacks complete, nonnegative integer
usage. `token_usage_status` distinguishes unknown from reported totals; real
reported zero remains zero. Known partial sums are diagnostic
`reported_token_usage`, not a complete total for a consumer token budget.

```sh
python -m pip install -e '.[test]'
pytest
```

Tests cover extracted transport edge cases, tool denial, path containment, artifact loading, routing, cancellation, and local HTTP/SSE integration. The end-to-end Prosaic test requires the CLI on PATH; CI installs the pinned Prosaic revision. Tests do not require paid LLM access. Successful conformance tests do not establish model quality; evaluate each agent/model pair against its own acceptance criteria.

Licensed under the [Apache License, Version 2.0](LICENSE). See [NOTICE.md](NOTICE.md)
for extraction provenance and [LICENSE-MIT](LICENSE-MIT) for the retained notice
covering historically MIT-licensed code. Third-party components retain their own licenses.

## Structured JSON tool transport

For endpoints where the host deliberately chooses JSON tool requests, use the
shared [`StructuredToolLoop`](docs/structured-tools.md). It reuses registered
`CustomTool` contracts and bounded execution, with application-owned final-result
admission. Harness can drive each turn without embedding product rules in Runtime.
