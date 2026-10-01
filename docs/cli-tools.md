# Custom command-line tools: discover, preflight, execute

The current setup uses Runtime v0.5.1+, Python Prosaic v0.3.0 (installed
automatically), and Harness v0.4.1+ for workflow integration. Runtime does not
require the Prosaic catalogue command to execute tools: it validates trusted manifests
itself. Agent Markdown is still parsed exclusively through `prosaic inspect`.

## Who does what?

- **Your application** installs the executable and owns its behavior.
- **Prosaic** describes tools with neutral YAML manifests and `tools: [name]`
  prose metadata. `prosaic tools` reads the catalogue; it never executes probes,
  finds executables, installs software, or grants permissions.
- **Runtime** loads explicitly trusted manifest directories, resolves executables,
  checks availability/permissions, and maps native function calls to fixed argv.
- **Harness** checks CLI availability when loading a workflow, and can require a
  successful version-matching native tool event before admitting an agent result.

Installing an executable alone does **not** expose it to a model. Discovery,
permission, availability, actual execution, and answer correctness are separate.
No MCP server, Python callback, general shell tool, or recursive agent is needed.

## 1. Create or install a CLI

Prefer a CLI that accepts explicit arguments, emits JSON on stdout, puts
diagnostics on stderr, has documented exit codes, and supports a short version
probe. Existing tools can be adapted without modifying their implementations.

The complete runnable sample is [prosaic-example-analyzer](../examples/cli-tool).
Install it in the same environment used for Runtime:

```sh
# From the prosaic-runtime repository root, after the normal environment setup.
source .venv/bin/activate
python -m pip install -e .
python -m pip install -e examples/cli-tool
prosaic-example-analyzer examples/evidence/requirements.md --json
```

For a uv-created environment without pip, the equivalent is
`uv pip install --python .venv/bin/python -e examples/cli-tool`.

The result is `{"requirements":2,"vague_ids":["REQ-002"],"passed":false}`.
Exit zero means the CLI ran successfully; `passed:false` is domain data, not a
process failure. This tiny synthetic wording check is **not** Understanding or a
comprehensive requirements-quality gate.

## 2. Describe the tool

Put a YAML/YML file in `.prosaic/tools/`:

```yaml
schema_version: 1
name: analyze_spec
tool_version: "1.0"
description: Analyze a specification and return a JSON report.
executable: prosaic-example-analyzer
argv: ["{spec}", "--json"]
parameters:
  type: object
  additionalProperties: false
  required: [spec]
  properties:
    spec: {type: string}
path_parameters: {spec: read}
version_probe: ["--version"]
version_contains: prosaic-example-analyzer 1.0
output_format: json
timeout_s: 10
max_output_bytes: 65536
```

The first version supports JSON output and string parameters only. Placeholders
must occupy an entire argv entry and name a required parameter. No shell syntax
is evaluated by the adapter; the model cannot select an executable, alter the argv
template or supply environment mappings. Values beginning with `-` and
NUL-containing values are rejected. Fixed literal flags are chosen by the manifest
author. The executable itself may interpret arguments as code or commands: a
manifest exposing `bash -c` or an evaluator would explicitly grant dangerous
execution capability and is not made safe by this adapter.

`path_parameters` currently supports `read` file arguments only. Runtime resolves
these against the invocation `cwd`, checks existence and host `read_roots` and
`forbidden_roots`, then passes absolute paths. It does not infer paths from
descriptions or grant read access automatically.

Names must not collide with Runtime builtins or other registered tools. Discovery
is non-recursive, sorted, and limited to 128 manifests across configured roots;
each manifest is at most 64 KiB. Unknown fields, duplicate YAML keys/names,
symlinked manifest files, external schema references, and invalid schemas fail
closed. Relative executable paths resolve against the manifest directory; bare
executable names resolve on host PATH at Runtime construction, then use that fixed
path. Recreate the Runtime after installing or relocating tools.

With the updated Prosaic CLI, inspect metadata without executing anything:

```sh
prosaic tools --source examples/.prosaic
# Or inspect a separately managed manifest directory:
prosaic tools --directory /absolute/path/to/tool-manifests
```

If another application's older Prosaic CLI is on PATH, activate the Runtime
environment and reinstall the package with its pinned Python dependency:

```sh
source .venv/bin/activate
python -m pip install .
prosaic --version
prosaic tools --source examples/.prosaic
```

This does not replace the Prosaic installation used by another application.

The JSON catalogue contains `schema_version:1` and `tools`, each with `path` and
`manifest`. Runtime still performs its own execution-specific validation.

## 3. Explicitly trust and grant tools

In operator-owned Runtime YAML:

```yaml
tool_directories: [.prosaic/tools]
allowed_tools: [analyze_spec]
# profiles, routes and limits as usual
```

Directories resolve against the YAML file's directory, not process cwd. There is
no default executable discovery or automatic loading of source directories.
Treat manifests and the directories containing them as executable code: review
changes before trusting them. A model-created manifest is not authorization.

In neutral Markdown:

```markdown
---
name: spec-reviewer
description: Explain deterministic analysis.
model_tier: balanced
tools: [analyze_spec]
---
ALWAYS call analyze_spec on the requested specification before explaining scores.
NEVER invent scores or claim execution without a successful tool result.
```

The host must independently grant `analyze_spec` and read scope through
`RunPolicy`, or CLI `--allow-tool analyze_spec --read-root evidence`.
The `read` and `write` aliases expand builtin tools only, not custom CLI tools.
Python-registered callbacks and CLI manifests can coexist, but name collisions
are errors. No changes are required for existing callback registrations.

## 4. Preflight without inference

From the Runtime repository root:

```sh
prosaic-runtime preflight \
  --config examples/cli-tools.yml --source examples/.prosaic \
  --agent subagents/cli-spec-reviewer.md --cwd examples \
  --allow-tool analyze_spec --read-root evidence --output text

# Check all registered/configured tools instead of one artifact:
prosaic-runtime preflight \
  --config examples/cli-tools.yml --all-tools --cwd examples \
  --allow-tool analyze_spec --read-root evidence --output text

# Equivalent example program; defaults to offline preflight:
python examples/run_cli_tool.py
```

Preflight checks registration, Runtime/host grants, executable availability,
explicit read scope, required environment names, and any configured version probe.
It neither contacts an endpoint nor needs an endpoint key. It does not install
tools. A trusted probe is executable code and may have side effects; omit
`version_probe` for existence-only checks. Probe output/stderr are never echoed.
`version_contains`, when provided, must match stdout from a successful probe.
Probes have a maximum five-second timeout, bounded output, and a shared preflight
deadline. `doctor` separately checks the endpoint; live smoke is a separate opt-in.

Selected-artifact preflight checks all explicitly requested tool names. Unused
missing executables do not block that selection, but appear in `--all-tools`.
Python callbacks can be checked for registration only; Runtime cannot infer their
external dependencies. Builtins are checked for grants and required path scope.
CLI tools requested by executable prose are also automatically preflighted before
the first model request. Missing permission is an error, not a silent downgrade.

## 5. Execute through native tool calling

```sh
source ~/.zshrc
export TOKENPROXY_KEY
python examples/run_cli_tool.py --live --profile qwen
python examples/run_cli_tool.py --live --profile ornith --no-stream
```

These commands contact the configured private-network endpoint and may incur
charges. The model requests `analyze_spec({"spec":"evidence/requirements.md"})`;
Runtime validates it, executes the fixed argv, and returns the CLI JSON inside
its normal `status/result` envelope. Tool events carry the manifest-bound version,
but omit raw arguments, process output, and exception text.

The sample requires the first native call to target `analyze_spec`, and separately
checks a successful event. `--initial-tool` enforces the first call attempt, not
successful execution or answer truth. The generic Runtime CLI also supports it:

```sh
prosaic-runtime subagents/cli-spec-reviewer.md \
  --config examples/cli-tools.yml --source examples/.prosaic --cwd examples \
  --allow-tool analyze_spec --read-root evidence --initial-tool analyze_spec --events
```

In Python, load the same YAML and call
`runtime.preflight(artifact, cwd=..., policy=...)`, then `runtime.run(...)`.
No custom Python registration is needed for manifests.

## Boundaries and failure behavior

CLI processes inherit only PATH/locale/platform essentials by default, not API
keys or unrelated host environment. `pass_env: [MY_TOOL_KEY]` explicitly forwards
named values from the invocation environment; missing values fail preflight.
There is no interactive stdin. Stdout and stderr share the configured byte cap
(128 bytes to 1 MiB); JSON must be UTF-8, finite, without duplicate keys, and
within the existing depth/result limits. Timeouts are finite, positive, and at
most 3,600 seconds. Nonzero exit is an operational failure unless the author
explicitly lists it in `success_exit_codes`, for example `[0, 1]` for a CLI whose
exit 1 accompanies a valid negative analysis report. Signals are never success.

Stable tool errors include `cli_unavailable`, `cli_environment`, `cli_version`,
`cli_arguments`, `cli_path_denied`, `cli_timeout`, `cli_output_limit`, `cli_exit`,
and `cli_invalid_output`. Ordinary tool errors are returned to the model, which
may explain them; they do not imply a successful tool event. Cancellation and
the shared invocation deadline terminate the CLI and stop further model requests.
On POSIX, cleanup terminates the process group, including ordinary children; on
Windows it terminates the direct child. Processes that deliberately escape their
group require stronger OS isolation.

This is **not an OS sandbox or complete prompt-injection prevention**. Read-scope
checks constrain declared path arguments, not every filesystem access the tool's
implementation might perform. Tools run with the host account's permissions and
can have effects. Tool output remains potentially untrusted content. Fixed argv,
schema/grant checks and credential isolation reduce attack surfaces; destructive
tools still need host approval and, where appropriate, a dedicated sandbox.
Interrupted or retried tools are not guaranteed exactly-once execution.

The version binds declared `tool_version`, normalized manifest semantics and
resolved executable path. It does **not** hash an executable's dependencies,
mutable data, or installed implementation. Bump `tool_version` whenever those
semantics change. Harness fingerprints include descriptors for used tools and
reject incompatible resume. Custom CLI events are not builtin file-read receipts;
`require_tools` alone does not certify which bytes were read or answer correctness.

## Optional: the actual Understanding CLI

An application-owned manifest/prose/config example lives under
[examples/understanding](../examples/understanding). It calls the independently
installed CLI as `understanding scan <spec> --basic --json`. Echelon is not imported
or installed by Prosaic Runtime. With `understanding` already on PATH:

```sh
prosaic-runtime preflight \
  --config examples/understanding/runtime.yml \
  --source examples/understanding/.prosaic --agent subagents/reviewer.md \
  --cwd examples --allow-tool understanding --read-root evidence --output text

prosaic-runtime subagents/reviewer.md \
  --config examples/understanding/runtime.yml --source examples/understanding/.prosaic \
  --cwd examples --allow-tool understanding --read-root evidence \
  --initial-tool understanding --events
```

The second command makes real model requests. Adapt and version-pin the manifest
for the actual CLI release you install. For a mandatory quality gate, prefer
controller-owned analysis/report validation rather than relying on prose alone.

If preflight reports `cli_version`, run the trusted executable's version command
directly to diagnose its installation. On this development Mac, the default PATH
entry for Understanding could not import its package; the executable in the
Echelon checkout's `.venv/bin` worked. We tested with that directory explicitly
on PATH without changing the application's host installation.

## Live verification snapshot: 2026-10-01

The synthetic CLI ran through native function calling on TokenProxy. In a single
four-model HTTP/SSE matrix, Ornith, DeepSeek and Nemotron each executed the tool
successfully in both modes (six successes). Qwen skipped the required initial
native call in both matrix requests; Runtime rejected them with
`tool_choice_not_honored` and no tool execution. A separate earlier Qwen SSE
request did execute successfully. These observations are not reliability
benchmarks, and no enforcement was relaxed or fallback automatically selected.

The live Harness/Qwen example executed the CLI, admitted the known synthetic
report, and stopped at human approval. A subsequent four-model Harness HTTP/SSE
matrix admitted seven of eight reports. Ornith/SSE returned the transport envelope
instead of the inner report and was correctly rejected. After clarifying that
distinction in the neutral role, a separate Ornith/SSE run admitted the report;
this is one observed improvement, not a reliability guarantee.
The optional actual Understanding CLI also
executed successfully through Runtime/Qwen SSE using the checkout's executable.
Transport/execution success does not certify its explanation: the model inferred
per-requirement completeness from aggregate metrics and made a concept-density
recommendation that needs independent review. Human review or application-owned
deterministic validation remains necessary.
