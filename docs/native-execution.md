# Native Claude Code and Codex execution

`NativeRuntime` is an opt-in extension for running the same canonical agent
through Claude Code or Codex CLI. It manages one assignment, profile selection,
bounded subprocess capture, cancellation and capacity cooldowns. It is separate
from `ProsaicRuntime`, which continues to own the direct API/tool loop.

The native path reads canonical prose through `prosaic inspect`, renders the
body and arguments, and sends the prompt to the vendor CLI on stdin. It does
not run `prosaic apply` or copy artifacts into `.claude` or `.codex` folders.

## Prerequisites and trust boundary

Install and authenticate each vendor CLI separately. Claude Code requires
**2.1.248 or later** for `--restricted`. Codex requires an `exec` implementation
supporting JSONL, `--ephemeral`, `--sandbox` and the global approval flag;
the command surface was checked against **0.154.0**. Unsupported flags fail;
Runtime never silently drops permission controls or disables the sandbox.
CLI installation, authentication and account access are operator responsibilities.

Execution requires an explicit `NativePolicy` (CLI: `--native-permissions`).
This accepts vendor-owned permissions and trusted native configuration. It does
not provide `RunPolicy` exact path grants, controlled API tool callbacks,
read receipts, initial-tool enforcement, per-request/token caps, accounting,
or portable conversation history. Native sessions are fresh per assignment.
NativeRuntime advertises only `native_profiles_v1` and `profile_pools_v1`, so
existing Harness admission cannot mistake it for an API runtime.

`read-only` uses Claude's explicit read tools or Codex's read-only sandbox.
`workspace-write` additionally enables Claude Edit/Write/Bash or Codex's
workspace-write sandbox. Claude permission rules are not an OS sandbox; in
particular, granting Bash is not exact filesystem/network confinement. Run in an
external container or other isolation when the workspace or tools are untrusted.
Native authentication, managed settings, native profile files, local services
and plugins remain trusted operator inputs. Do not configure startup hooks with
effects that would be unsafe to repeat. Runtime cannot prove that unreported
effects outside the native event stream did not occur.

Claude restricted mode avoids loading user/project settings and auto-discovered
customizations; Runtime supplies an empty strict MCP configuration and disables
skills. An explicit Claude settings file and managed settings are still trusted.
Codex loads its normal native configuration, optionally a named profile, with
explicit final sandbox and approval settings. Those native settings may contain
MCP integrations or other trusted functionality. The explicit sandbox restricts
native command execution; it does not sandbox external MCP services.

## Configuration

Use a separate `kind: native` file. Existing API YAML and on-disk formats are
unchanged. See [`examples/native-runtime.yml`](../examples/native-runtime.yml).

```yaml
kind: native
profiles:
  claude-main:
    backend: claude-code
    model: sonnet
    quota_key: claude-personal
    permission_mode: workspace-write
  codex-main:
    backend: codex-cli
    model: your-accessible-codex-model
    quota_key: codex-personal
    permission_mode: workspace-write
pools:
  coding:
    profiles: [claude-main, codex-main]
    strategy: prefer-first-available
    cooldown_s: 300
default_pool: coding
routes:
  fast: coding
  balanced: coding
  strong: coding
capacity_file: .native-capacity.json
```

`quota_key` is a non-secret operator identifier for shared capacity. Profiles
using the same account and limit bucket must share this key. Renaming a profile
does not create more quota. For providers with overlapping account/model limits,
use a conservative account-wide key. Pool selection is priority ordered, not
random load balancing. Unknown or expired availability is eligible.

Optional profile fields are `executable`, `effort`, `supported_efforts`,
`config_dir`, Codex-only `native_profile`, and Claude-only `settings_file`.
The configuration directory becomes `CODEX_HOME` or `CLAUDE_CONFIG_DIR` in the
child's environment; Runtime does not read or copy credentials. Native profiles
do not log in or switch accounts implicitly. Each configuration directory must
already have the intended authentication.

Paths in YAML are relative to the YAML file, including `capacity_file` and
executables containing `/`. Bare executable names resolve through PATH. Explicit
artifact effort overrides the configured profile default; when omitted, the
profile effort applies. When both are omitted, the native CLI/model default applies.
Explicit `profile` or `pool` selection overrides `model_tier` routing. Model
identifiers belong in operator profiles, not canonical agent prose.

## Reasoning effort from prose

Set `effort` in canonical Markdown frontmatter:

```markdown
---
name: reviewer
description: Review supplied code
effort: high
---
Review {{args}} and report the findings.
```

The neutral effort values are `low`, `medium`, `high`, `xhigh`, `max`, and
`ultra`. Use these spellings in canonical Markdown and Runtime configuration;
picker labels such as Extra High are written as `xhigh`. Runtime maps an
admitted value to the selected provider's setting:

| Execution adapter | Native request setting | Available neutral values |
| --- | --- | --- |
| OpenAI-compatible API | Chat Completions JSON `reasoning_effort` | `low`, `medium`, `high`, `xhigh`, `max` |
| Claude Code native | `--effort <value>` | `low`, `medium`, `high`, `xhigh`, `max` |
| Codex CLI native | `-c 'model_reasoning_effort="<value>"'` | `low`, `medium`, `high`, `xhigh`, `max`, `ultra` |

Effort support is resolved automatically from the selected provider and model.
For higher-effort native runs without an explicit override, Runtime reads Codex
`model/list` metadata or Claude's SDK initialization model information. These
handshakes send no prose, create no model thread, and start no inference turn.
They honor the profile's executable, native profile/settings file, configuration
directory and invocation environment.

Discovery runs lazily for the current eligible pool member, after policy and
cooldown checks. It never probes unused fallback members. A shared invocation
deadline/cancellation applies, with at most three seconds per metadata probe,
1 MiB of output and four Codex model-list pages. Successful results are cached
in memory for 60 seconds, with a 64-entry bound and isolation by profile, working
directory and environment digest. Failed or cancelled probes are not cached.
Host cancellation-callback exceptions propagate and stop the invocation; they
are not treated as unavailable metadata eligible for fallback.

Standard OpenAI-compatible model responses do not define effort capabilities;
vendor extensions such as DeepSeek's model catalogue are not queried by API
admission. Runtime includes dated, exact-model defaults for documented OpenAI,
DeepSeek V4, Qwen 3.8, Nemotron NIM and native model IDs; it does not infer
support from arbitrary name prefixes. Native advertisements take precedence
over those offline defaults.
Dynamic Claude aliases such as `sonnet` use CLI metadata rather than a guessed
latest model. Pure API validation and conversation preparation perform no
network or process discovery. Unknown models retain legacy low/medium/high
support; higher levels require usable metadata or an explicit override. Existing
API `features.reasoning_effort: xhigh` or `max` defaults remain operator assertions
for unknown/custom models, preserving Runtime 0.9.9 behavior. A model override
cannot inherit that assertion, and known-model capabilities remain authoritative.

For a custom/unknown model or a restricted deployment, optionally override its
capabilities with `supported_efforts`:

```yaml
profiles:
  codex-deep:
    backend: codex-cli
    model: your-model-with-ultra-support
    quota_key: codex-account
    supported_efforts: [low, medium, high, xhigh, max, ultra]
    effort: medium
```

API profiles accept the same `supported_efforts` field next to `model` and
`base_url`; their default effort stays under `features.reasoning_effort`.
Python constructors accept a tuple or list and own an immutable tuple copy.
Omitting the declaration uses automatic detection/defaults. An empty list
declares a model without effort support. An explicit declaration takes
precedence over automatic results and avoids native metadata discovery. Alias
changes, CLI upgrades, account restrictions, and provider-side caps can affect
availability; unknown custom API models may still need an override because the
API protocol has no standard capability fields. A declaration must be a
subset of the adapter's available values: declaring `ultra` on Claude or an
OpenAI-compatible API profile is rejected.

Explicit artifact effort takes precedence over the profile default. For API
profiles the default is `features.reasoning_effort`; for native profiles it is
`effort`. Omitting both leaves the setting out of the request/command. Runtime
rejects unsupported artifact values before dispatch. Native and unified pools
skip profiles that cannot represent the requested effort, with
`profile_skipped` reason `effort_unavailable`; these are not provider attempts
and do not consume a quota cooldown. Direct execution rejects incompatible
requests. Fallback resolves effort independently for each eligible profile
without changing configured defaults. Runtime does not clamp a requested value
to a lower one. Vendor-side caps or fallback may still affect the actual effort;
declare only the levels supported by the selected model and installed client.

Neutral `ultra` is Codex-only and can enable its native subagents. Claude's
`ultracode` workflow mode is separate and is not mapped or enabled by Runtime.
Claude execution retains the existing read-only/workspace-write tool list at
every supported effort. No `Workflow` or `Agent` grants are added by effort.

Direct Anthropic API execution is a separate adapter and continues to reject
effort controls in this version. Claude Code native execution uses its CLI flag.

The mappings follow the [OpenAI reasoning API documentation](https://developers.openai.com/api/docs/guides/reasoning),
[Claude CLI reference](https://code.claude.com/docs/en/cli-reference), and
[Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).
Metadata sources: [Codex model/list](https://learn.chatgpt.com/docs/app-server#list-models-modellist),
[Claude ModelInfo](https://code.claude.com/docs/en/agent-sdk/typescript#modelinfo),
and [standard OpenAI model fields](https://developers.openai.com/api/reference/resources/models/methods/retrieve).
See also [Claude effort](https://code.claude.com/docs/en/model-config#adjust-effort-level)
and [Codex Max and Ultra](https://learn.chatgpt.com/docs/models).

## CLI and Python

```sh
prosaic-runtime subagents/reviewer.md --source examples/.prosaic \
  --config examples/native-runtime.yml --cwd /path/to/workspace \
  --pool coding --native-permissions read-only --events

prosaic-runtime subagents/reviewer.md --source examples/.prosaic \
  --config examples/native-runtime.yml --cwd /path/to/workspace \
  --profile codex-main --native-permissions read-only
```

Replace the example Codex model before using it. These commands invoke paid or
subscription-backed inference through your existing CLI login.

```python
from prosaic_runtime import NativeRuntime, NativeRuntimeConfig, NativePolicy

runtime = NativeRuntime(NativeRuntimeConfig.load('native-runtime.yml'))
result = runtime.run('subagents/reviewer.md', 'Review the current changes',
                     cwd='/path/to/workspace', pool='coding',
                     policy=NativePolicy(permission_mode='read-only', timeout_s=180))
```

Both the profile and the host policy must permit writes. Artifact declarations
using `tools: full` are supported for native coding roles only when both grant
workspace writes; direct API execution continues to reject `full`. Declarations
requesting unsupported custom API tools are rejected; native tools remain owned
by the vendor. API grant/accounting/round-limit CLI flags are rejected in native
mode. `doctor`, `smoke`, `preflight`, `conformance`, `turn()` and RuntimeAdapter
retain their existing API contracts; native integration does not advertise them.

## Capacity and recovery

The pool tries each eligible profile at most once per invocation. A shared
absolute deadline covers inspection, selection and attempts. Missing executables
and recognized native rate/usage/overload failures put the quota key into
cooldown. A reported reset timestamp is used when available, otherwise the pool
cooldown applies. A later shorter cooldown cannot erase an existing longer one.
Successful tasks do not clear another worker's exhaustion observation.

Automatic fallback requires a recognized terminal capacity failure and no
observed model text, reasoning (including redacted thinking), started model
stream, or tool/command/file/hook activity. Malformed or incomplete streams,
authentication errors, timeouts, cancellation and unknown failures do not cause
replay. Claude's completed failure must also report zero agent turns. This is
deliberately conservative: a limit interruption after work returns
`recovery_required: true`. The host/Harness must reconcile changes and supply a
fresh assignment/context pack before another backend continues. Native session
IDs in metadata are diagnostic; they cannot be transferred between vendors.

The normalizer accepts Claude `rate_limit_event` rejection and supported typed
errors. Codex exec currently exposes message-only errors, so known provider
usage-limit prefixes are recognized inside error envelopes. Unknown/localized
messages remain failures without automatic fallback. Agent output and arbitrary
stderr are never searched for quota instructions.

If every eligible profile is cooling down, execution returns
`failure_reason: capacity_unavailable` with the earliest known `retry_at` epoch
timestamp. Runtime does not sleep or queue indefinitely. The application owns
future scheduling. The current extension does not call an undocumented Claude
subscription endpoint or implement proactive Codex app-server polling.

Hosts with a supported availability signal can update `runtime.capacity`:

```python
runtime.capacity.mark_unavailable('claude-personal', reason='usage_limit', until=reset_epoch)
runtime.capacity.mark_available('claude-personal')  # Explicit authoritative refresh.
```

Without `capacity_file`, cooldowns live in the reusable Runtime instance.
With it, atomic writes and POSIX advisory locking share cooldowns between
processes. Place it in a trusted local directory; it is operational scheduling
state, not an accounting ledger or a security boundary. Do not use it on storage
without reliable advisory locks. Corruption fails closed. Concurrent tasks can
use both profiles, but availability observations do not reserve provider quota.
Hosts must serialize or isolate writes to the same workspace.

Results retain bounded per-attempt metadata and known token totals under
`native_execution_v1`. Total `token_usage` is unknown if any launched attempt
lacks usage. Codex cached input tokens are already included in input totals;
Claude cache read/creation counts are additional. Monetary costs are unavailable.
Native progress events expose event types, not raw prompts, commands or file text.
Critical callback failures stop execution and clean up the owned process group.

## References

- [Claude CLI reference](https://code.claude.com/docs/en/cli-reference)
- [Claude programmatic execution](https://code.claude.com/docs/en/headless)
- [Codex non-interactive execution](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Codex CLI reference](https://learn.chatgpt.com/docs/cli/reference)
