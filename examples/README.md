# Run Prosaic prose with and without tools

These examples use the same runtime in two modes:

| Example | Input | Granted tools | Output |
| --- | --- | --- | --- |
| Summarizer | Text supplied as arguments | None | Three-sentence summary |
| Evidence reviewer | A checked-in evidence file | `read_file`, inside `evidence/` only | Findings with source references |

## Setup

Install the runtime and Prosaic CLI using the [installation instructions](../README.md#install).
Clone this repository if you do not already have it, then enter its examples directory:

```sh
git clone https://github.com/B3Cognition/prosaic-runtime.git
cd prosaic-runtime/examples
```

All commands below run from this directory. Edit `base_url` and `model` in both
[prosaic-runtime.toml](prosaic-runtime.toml) and
[with-tools.toml](with-tools.toml) to match your Chat Completions endpoint.
The supplied localhost URL and `your-model` are placeholders, not a hosted service.
If authentication is required, set `LOCAL_LLM_API_KEY` in your environment; do not
put credentials in the example files. Calls can incur your provider's normal fees.

The tool example requires a model and endpoint that support function calling.
Both configurations enable streaming. If your endpoint rejects streaming usage,
set `stream_options = false`; if it does not stream, set `streaming = false`.
The summarizer declares `effort: low`; remove that line if your endpoint does not
accept `reasoning_effort`.

## 1. Prose without tools

The [summarizer prose](.prosaic/subagents/summarizer.md) receives all its input
through `{{args}}`. It has no tool declaration, the configuration grants no tools,
and the invocation supplies no tool permissions.

```sh
prosaic-runtime subagents/summarizer.md \
  --arguments 'S1: The pilot processed 120 requests. S2: Three requests timed out. S3: The cause has not been established.'
```

The command prints a JSON result. A successful result has `exit_code: 0`; its
`stdout` contains the model's summary. Expect the three observations and their
source identifiers to be preserved, without inventing a timeout cause. Wording
varies by model. No file-reading or other tool is offered to the model.

Add `--events` to receive JSONL events followed by the final result:

```sh
prosaic-runtime subagents/summarizer.md \
  --arguments 'S1: The pilot processed 120 requests. S2: Three requests timed out.' \
  --events
```

## 2. Prose with a read-only file tool

The [reviewer prose](.prosaic/subagents/reviewer.md) declares `tools: read` and
instructs the model to read [evidence/pilot.md](evidence/pilot.md). The configuration
allows only `read_file`, and the invocation grants that tool only within `evidence/`:

```sh
prosaic-runtime subagents/reviewer.md \
  --config with-tools.toml \
  --arguments 'Review evidence/pilot.md. What do we know, and what remains unknown?' \
  --allow-tool read_file \
  --read-root ./evidence \
  --events
```

Expect a `read_file` call followed by a final answer covering observed results,
unknowns, and a next check, with references to `evidence/pilot.md` and its source
identifiers. The runtime executes the read and returns the file content to the
model; the prose alone does not grant access. This example cannot write files,
run shell commands, or spawn agents.

The effective tool grant is the intersection of three independent declarations:

1. Prose: `tools: read` requests read capabilities.
2. Configuration: `allowed_tools = ["read_file"]` limits available tools.
3. Invocation: `--allow-tool read_file --read-root ./evidence` grants access for this run.

Omitting `--allow-tool` offers no tools. Granting it without `--read-root` offers
the tool but denies file access. Files outside `evidence/` remain inaccessible.
The agent is instructed to report denied access rather than invent findings;
following that instruction is model behavior, not an output-validation guarantee.

## Python equivalents

Run this from `examples/` after configuring the same endpoint files:

```python
from prosaic_runtime import ProsaicRuntime, RunPolicy

plain = ProsaicRuntime.from_config("prosaic-runtime.toml")
summary = plain.run(
    "subagents/summarizer.md",
    arguments="S1: 120 requests. S2: Three timeouts. S3: Cause unknown.",
    policy=RunPolicy(timeout_s=60),
)
if summary.exit_code:
    raise RuntimeError(summary.stderr)
print(summary.stdout)

reader = ProsaicRuntime.from_config("with-tools.toml")
review = reader.run(
    "subagents/reviewer.md",
    arguments="Review evidence/pilot.md. What remains unknown?",
    cwd=".",
    policy=RunPolicy(
        allowed_tools=frozenset({"read_file"}),
        read_roots=("evidence",),
        timeout_s=60,
        max_tool_rounds=4,
    ),
)
if review.exit_code:
    raise RuntimeError(review.stderr)
print(review.stdout)
```

`exit_code: 0` confirms execution completed, not that the answer is correct.
Evaluate the output against the evidence for your chosen model. The automated
example tests use a local HTTP fixture and real Prosaic inspection; they validate
the wiring and permission behavior without paid inference.
