# Run Prosaic prose with and without tools

## Host-registered custom tools

This development example requires this feature checkout, Python 3.11+ and the
Prosaic CLI installed using the root first-run guide. Do not checkout the release
tag for this example. From the repository root:

```sh
source .venv/bin/activate
python -m pip install -e .
source ~/.zshrc
export TOKENPROXY_KEY
python examples/run_custom_tool.py --live --config examples/custom-tools.yml --profile qwen
python examples/run_custom_tool.py --live --profile ornith --no-stream
python examples/run_custom_tool.py --live --profile deepseek
python examples/run_custom_tool.py --live --profile nemotron
python examples/run_custom_tool.py --live --sku SKU-999
```

The fixed [catalog_tools.py](catalog_tools.py) registers `lookup_catalog` over
[catalog.json](catalog.json). A version includes the exact catalogue byte hash.
YAML is configuration, not an importer: `tools: [lookup_catalog]` in neutral prose,
`allowed_tools: [lookup_catalog]` in YAML and `RunPolicy.allowed_tools` on the host
must all grant it. Registration alone grants nothing. `read`/`write` aliases
expand builtins only. For another endpoint use `--base-url`/`--model`, or edit a
copy of the YAML; credentials stay in the named environment variable.

The program explicitly requests lookup_catalog on the first model turn and
prints final Result plus structural versioned events. Missing grants/registration
fail before execution; bad SKU/unknown arguments return `invalid_arguments`,
denied authorization returns `authorization_denied`, callback exceptions return
`handler_error` without exception text. An absent valid SKU returns `found:false`.
Transport success does not prove a correct final answer; the Harness companion
adds deterministic catalogue admission and an explicit human choice.

Handlers return data inside a Runtime-owned `status`/`result` envelope. Default
argument/result limits are 16/64 KiB with finite JSON and at most 64 container
levels. Fixed error envelopes remain valid JSON independently (under 256 bytes).
Callbacks are trusted in-process Python, not a sandbox: boundary checks cannot
kill a hung handler, undo its effects or guarantee exactly-once execution.
Consequential handlers need a real host approval predicate; never trust an
`approved` field supplied by the model. Change the tool version whenever semantics
or fixed data change; version is a host assertion, not automatic source hashing.

## Staged acquisition and explicit preloading

This opt-in API requires Runtime v0.4.0 or newer.
Install this checkout into your environment first (`python -m pip install .`),
with the Prosaic CLI on PATH as described in the root README. Export
`TOKENPROXY_KEY` from your shell configuration; no key belongs in YAML.

```sh
python examples/run_acquisition.py --config examples/tokenproxy.yml --profile qwen
python examples/run_acquisition.py --config examples/tokenproxy.yml --profile ornith --mode staged --no-stream
python examples/run_acquisition.py --config examples/tokenproxy.yml --profile deepseek --mode preloaded
python examples/run_acquisition.py --config examples/tokenproxy.yml --profile nemotron --mode staged
```

The default `both` mode runs two independent examples. The staged invocation
first sends `acquire-pilot.md`; after a successful native `read_file`, it appends
`acquired-reviewer.md` and final arguments to the same conversation. No final
prose/arguments reach the endpoint before acquisition. Its policy permits only
reading `evidence/pilot.md`, with one shared timeout and tool-round budget.
The preloaded invocation explicitly reads the fixed sample file on the host and
passes its text to `preloaded-reviewer.md` with no tools. It does not pretend a
native read occurred, and is never used automatically after a staged failure.

For your own endpoint, configure `with-tools.yml`, or use `--base-url` and
`--model` overrides. The script prints a result JSON object per example and
stops on an unsuccessful result. Success confirms transport completion, not
answer truth; use the companion Harness examples for schema/source admission.

These examples use the same runtime in two modes:

| Example | Input | Granted tools | Output |
| --- | --- | --- | --- |
| Summarizer | Text supplied as arguments | None | Three-sentence summary |
| Evidence reviewer | A checked-in evidence file | `read_file`, inside `evidence/` only | Findings with source references |

## Setup

For a first installation, follow the complete [first-run guide](../README.md#first-run-from-installation-to-an-answer),
which brings you into this directory with both CLIs installed and the endpoint configured.
If you already have both CLIs installed but do not have the example files, clone this repository:

```sh
git clone https://github.com/B3Cognition/prosaic-runtime.git
cd prosaic-runtime/examples
```

All commands below run from this directory. Edit `base_url` and `model` in both
[prosaic-runtime.yaml](prosaic-runtime.yaml) and
[with-tools.yml](with-tools.yml) to match your Chat Completions endpoint.
The supplied localhost URL and `your-model` are placeholders, not a hosted service.
If authentication is required, set `LOCAL_LLM_API_KEY` in your environment; do not
put credentials in the example files. Calls can incur your provider's normal fees.

The tool example requires a model and endpoint that support function calling.
Run `prosaic-runtime doctor` to check setup without inference. Run
`prosaic-runtime smoke --live --config with-tools.yml` for the packaged live
capability test. Both example configurations set a 180-second invocation timeout;
override it with `--timeout` when needed.
Both configurations enable streaming. If your endpoint rejects streaming usage,
set `stream_options: false`; if it does not stream, set `streaming: false`.
The summarizer declares `effort: low`; remove that line if your endpoint does not
accept `reasoning_effort`.

## TokenProxy endpoint

[tokenproxy.yml](tokenproxy.yml) configures the private-network endpoint
`http://10.16.81.27:8080/v1` and reads credentials from `TOKENPROXY_KEY`.
Run these commands from the **repository root**, using its virtual environment
explicitly to avoid picking up an older runtime installed with another application:

```sh
source ~/.zshrc
export TOKENPROXY_KEY

.venv/bin/prosaic-runtime doctor --config examples/tokenproxy.yml --output text
.venv/bin/python examples/run_examples.py --config examples/tokenproxy.yml --events
.venv/bin/prosaic-runtime smoke --live --config examples/tokenproxy.yml --output text
```

The sample runs both no-tool and read-only-tool prose. The smoke command additionally
asserts that the tool example actually executes `read_file`. No key is stored in YAML.

The profiles are `qwen` (agents/tools), `ornith` (general chat/code), `nemotron`
(operator-recommended for contexts over 262,144 tokens), and `deepseek` (comparison
only). These use cases come from the endpoint operator, not runtime benchmarks.
The experimental tier mapping is:

| Prose tier | Profile | Model |
| --- | --- | --- |
| `fast` | `ornith` | `ornith-1.5-35b` |
| `balanced` | `qwen` | `qwen36-35b-a3b` |
| `strong` | `deepseek` | `deepseek-v4-flash` |
| `ultra` | `nemotron` | `nemotron-3.5-lightning` |

This is a configurable policy, not a verified speed/quality ranking or automatic
escalation. `ultra` selects the largest advertised context window, not proven
strongest reasoning; the operator recommends Nemotron only for contexts over
262,144 tokens. `strong` uses the operator's comparison-only DeepSeek pool.
The existing examples both declare `fast`, so both now select Ornith. Prose without
a tier still defaults to Qwen. Diagnostics and smoke tests use `default_profile`
(Qwen) unless you pass `--profile ornith`, `--profile deepseek`, or `--profile nemotron`.

For an explicit comparison run, override the model for both examples:

```sh
.venv/bin/python examples/run_examples.py --config examples/tokenproxy.yml \
  --model ornith-1.5-35b --events
```

`--model` overrides every profile for that sample run, so omit it when testing tier
routing. Alternative model IDs are listed in the YAML. Tool/reasoning/streaming
compatibility must be verified per model; Qwen is the previously live-tested model.
Advertised context sizes are comments only: they do not raise runtime safety limits,
and `max_tokens` limits output, not the model's context window.

## Four-tier launch dossier demo

[run_tiers.py](run_tiers.py) runs four independent Prosaic Markdown subagents against
a fictional, approximately 5,100-word launch dossier in [evidence/launch](evidence/launch).
It includes a proposal, measurements, interviews, incident notes, conflicting
headlines, a quoted instruction to ignore, and late evidence affecting the decision.
All content is synthetic; no workplace files are sent to the endpoint.

| Tier | Markdown subagent | Input and capabilities |
| --- | --- | --- |
| `fast` | [launch-briefer](.prosaic/subagents/launch-briefer.md) | Supplied proposal (~1,700 words); no tools |
| `balanced` | [launch-analyst](.prosaic/subagents/launch-analyst.md) | Reads the three dossier files; computes rates and reports limitations |
| `strong` | [launch-skeptic](.prosaic/subagents/launch-skeptic.md) | Reads the dossier; checks competing claims and missing evidence |
| `ultra` | [launch-decision](.prosaic/subagents/launch-decision.md) | Full dossier supplied directly; synthesizes a conditional decision without tools |

From the **repository root**, with both Prosaic and the runtime installed:

```sh
source ~/.zshrc
export TOKENPROXY_KEY

# All four models, sequentially. Makes real requests and may incur charges.
.venv/bin/python examples/run_tiers.py

# A single tier, or JSONL events including streamed text and final results.
.venv/bin/python examples/run_tiers.py --tier balanced
.venv/bin/python examples/run_tiers.py --tier strong --events --timeout 180
```

The default config is `examples/tokenproxy.yml`, resolved relative to the script.
Use `--config path/to/config.yml` for another endpoint; paths supplied explicitly
are relative to your working directory. There is deliberately no model override:
the Markdown tier selects a YAML route. The runner prints the actual selected
profile/model, answer, elapsed time, reported token usage (or `None` if unavailable),
observed streaming, and whether a read tool executed. `--events` reserves stdout
for JSONL. The timeout applies separately to each agent, including its tool loop.

Balanced and strong have only `read_file` access under `evidence/launch`; fast
and ultra have no tools. Config permission alone does not grant a tool: prose,
configuration, and invocation permissions must all agree. Runs are independent,
not recursive agents or a chain that passes one model's answer into another.
No file-writing or shell tools are granted. Execution stops on the first failed
run; use `--tier` to test another model independently after a failure.

Exit zero requires nonempty completed answers and at least one successful
`read_file` for each tool-based example. This checks execution, **not answer
quality or proof that every requested source was read**. Inspect the answers:

- Metrics should distinguish the 2.5% timeout rate, 3.5% combined failure rate,
  and misleading 99% headline (M01–M02).
- Reviews should retain sample/denominator caveats, not double-count F04, and
  reject the quoted instruction in F09 as authority.
- Full-dossier synthesis should account for the late rollback correction and
  unassigned evening coverage (F14–F15), without inventing approval.

These are different jobs, not a controlled quality/speed comparison. Nemotron's
`ultra` route demonstrates configuration, not superior reasoning or a million-token
capacity test. The dossier is far smaller than 262,144 tokens; the operator's
recommendation to reserve Nemotron for very large contexts still applies outside
this explicit demo. A larger advertised context does not increase runtime limits.

### Live verification snapshot (2026-09-30)

One sequential run against the configured TokenProxy endpoint completed all four
jobs with observed streaming. These are single-run observations, not benchmarks:

| Tier/model | Elapsed | Reported tokens | Successful file reads |
| --- | --- | --- | --- |
| fast / Ornith | 1.83 s | 2,740 | 0 (no tools) |
| balanced / Qwen | 4.01 s | 10,891 | 3 |
| strong / DeepSeek | 12.30 s | 10,368 | 3 |
| ultra / Nemotron | 14.14 s | 10,236 | 0 (full text supplied) |

Manual review found important quality limitations despite successful execution:
DeepSeek described the 1% versus 3.5% failure comparison as approximately 2.5×
(the ratio is 3.5×; the difference is 2.5 percentage points), and had an incorrect
source prefix. Nemotron exceeded the requested 600-word limit and described some
proposed gates as mandatory, despite the instruction to preserve proposal status.
It did incorporate the late F14/F15 evidence. Neither tool-based model followed
every citation-format instruction. Human review remains necessary; the runner
does not validate arithmetic, citations, word limits, or policy interpretation.

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

Alternatively, add `--output text` for only the answer. Status stays on stderr;
`--quiet` suppresses it. Do not combine `--output` with `--events`.

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
  --config with-tools.yml \
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
2. Configuration: `allowed_tools: [read_file]` limits available tools.
3. Invocation: `--allow-tool read_file --read-root ./evidence` grants access for this run.

Omitting `--allow-tool` offers no tools. Granting it without `--read-root` offers
the tool but denies file access. Files outside `evidence/` remain inaccessible.
The agent is instructed to report denied access rather than invent findings;
following that instruction is model behavior, not an output-validation guarantee.

## Runnable Python sample

The complete [run_examples.py](run_examples.py) program runs both examples through
the public Python API. After setup, run from the repository root:

```sh
source .venv/bin/activate
export LOCAL_LLM_API_KEY="$(python -c 'import getpass; print(getpass.getpass("API key: "))')"
python examples/run_examples.py
```

For an unauthenticated endpoint, omit the export. This makes real model requests
and may incur charges. The script finds prose, configuration and evidence relative
to its own file, so it also works when launched from another directory.

Override the endpoint and model without editing the YAML files:

```sh
python examples/run_examples.py \
  --base-url http://127.0.0.1:8000/v1 \
  --model your-model \
  --timeout 180 \
  --events
```

Replace the placeholder URL/model with your endpoint's values. `--config path/to/local.yml`
uses one configuration for both examples; it must allow `read_file` for the reviewer.
Explicit configuration paths are relative to your working directory. Endpoint/model
overrides are applied only in memory. Otherwise, the script uses the two checked-in
YAML files and their configured time/round limits.

By default, stdout contains one JSON result per example; status goes to stderr.
`--events` adds JSONL runtime events tagged with the example name. The script stops
on the first failure and exits nonzero. Exit 0 means both executions completed;
it does not guarantee answer quality or prove the model used its tool—use
`prosaic-runtime smoke --live` for capability assertions. No API key is stored.

To embed the same calls in your application:

```python
from prosaic_runtime import ProsaicRuntime, RunPolicy

plain = ProsaicRuntime.from_config("prosaic-runtime.yaml")
summary = plain.run(
    "subagents/summarizer.md",
    arguments="S1: 120 requests. S2: Three timeouts. S3: Cause unknown.",
    policy=RunPolicy(timeout_s=180),
)
if summary.exit_code:
    raise RuntimeError(summary.stderr)
print(summary.stdout)

reader = ProsaicRuntime.from_config("with-tools.yml")
review = reader.run(
    "subagents/reviewer.md",
    arguments="Review evidence/pilot.md. What remains unknown?",
    cwd=".",
    policy=RunPolicy(
        allowed_tools=frozenset({"read_file"}),
        read_roots=("evidence",),
        timeout_s=180,
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
