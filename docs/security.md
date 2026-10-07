# Prompt-injection containment: first audit, 2026-10-01

## Release status — 2026-10-07

Runtime 0.5.2 includes the opt-in CLI sandbox described below and the independent
structured JSON tool loop. CLI isolation remains off by default. Required mode
fails closed; Python callbacks, validators and native coding providers are not
isolated by it. The dated verification receipts below describe earlier development
snapshots; their statements about uncommitted/unreleased code are historical.

## Linux ARM64 follow-up

Required-mode CLI isolation now also has a Linux Bubblewrap backend. It mounts
a synthetic read-only root and only approved dependencies/evidence, keeps writable
scratch and deny-mask backing files separate, drops capabilities, disables nested
user namespaces, and isolates PID/IPC/network/UTS namespaces. Kernel setup and
Bubblewrap >= 0.12.0 are checked before inference, even without a version probe.
The minimum version addresses the upstream
[setup-escape advisory](https://github.com/containers/bubblewrap/security/advisories/GHSA-pxhw-h44j-8pfx).

Real ARM64 Linux process tests exercise host/alias read denial, non-scratch write
denial, loopback denial, readable evidence, scratch cleanup, inherited child
policy, forbidden files/directories and version-probe isolation. Runtime and
Harness full suites and wheel/source builds were run inside a disposable ARM64
Linux container. Its outer fixture allows namespace setup; the tested CLI still
runs through Bubblewrap with dropped capabilities, and targeted containment tests
also pass as an unprivileged UID. No endpoint credential or real model was used.

Final receipt: **311 Runtime tests passed on Linux ARM64 and macOS**, and **119
Harness tests passed on each** against the development Runtime. The **20 targeted
Runtime containment/probe cases** also passed as UID 1000, both with the system
interpreter and a Python virtualenv; all **6 Harness sandbox cases** passed as
UID 1000. Runtime and Harness wheel/source builds succeeded on both platforms
(existing manifest exclusion warnings remain). Linux used Bubblewrap 0.12.0 and
Python 3.12. No global application install, version or dependency pin changed.

CI adds native Ubuntu ARM64 and AMD64 jobs with a pinned patched Bubblewrap build
and a mandatory backend gate. These remote jobs have not run yet; AMD64 remains
unverified locally. AppArmor/kernel/container restrictions may reject setup;
Runtime fails closed rather than relaxing them. Do not mount host Unix sockets
through evidence/runtime grants: Linux network namespaces alone do not block
pathname Unix sockets. Native coding providers and Python callbacks are unchanged.
This remains uncommitted, unreleased working-tree functionality.

## Follow-up: CLI isolation implementation

The working tree now implements opt-in `cli_sandbox.mode: required` on macOS,
including version probes. Real OS tests deny unrelated/symlinked host reads,
non-scratch writes and loopback connections, preserve evidence access and private
scratch, and verify child inheritance and forbidden-root precedence. An off-mode
positive control confirms the canary and connection would otherwise succeed.
Harness binds required-mode adapter policy and approvals to workflow identity.

The synthetic local-endpoint probe completed successfully on 2026-10-01:
`preflight_ok=true`, `probe_executed=true`, `containment_passed=true`, exit code 0,
864 reported tokens. No real secret was read or saved. The model client retained
its credential; the CLI child did not inherit it. This is one containment trial,
not a model-injection resistance benchmark or proof of native-provider isolation.

The earlier audit below describes the unsandboxed behavior, which remains the
default for compatibility. The initial implementation was macOS-only; Linux
support is described above. Python callbacks/validators and Echelon's
native providers remain outside this boundary. See [setup](cli-tools.md) and
[decision/remaining work](adr-cli-sandbox.md).

Fresh local verification of this follow-up: **305 Runtime tests passed**, **119
Harness tests passed** against the development Runtime, and both wheel/source
builds succeeded. Runtime CI now includes macOS for actual Seatbelt tests, plus
Linux for general/unsupported-backend coverage; these new remote jobs have not
run yet. Existing versions/pins/global installations were not changed in this
follow-up. The feature remains uncommitted and unreleased.

This is a bounded source review and deterministic adversarial test pass, not a
security certification or live-model red-team result. The test model deliberately
obeys injected instructions. Passing therefore measures execution containment,
not model resistance to persuasion. No real credentials or remote model calls
are used. The testing strategy prioritizes boundary/integration checks over
prompt wording and pairs protected cases with residual-risk characterization.

## Trust model and ownership

Treat repository contents, retrieved evidence, tool descriptions/results, model
text, and proposed tool arguments as potentially hostile. In particular, text
claiming to be a system message, operator permission, or human decision is data.
Host configuration, installed executables, custom handlers, validator code,
workflow definitions and local controller storage remain trusted inputs. An
attacker who can alter these trusted inputs is outside the narrow prompt-only
threat model. Checksum binding is not authentication against such an attacker.

| Boundary | Owner | What must remain deterministic |
| --- | --- | --- |
| Markdown/catalogue discovery → artifact | Prosaic | Parse/render/discover without executing catalogue manifests |
| Artifact/model call → tool execution | Runtime | Intersection of declared, configured and host-granted tools; scoped builtin paths; bounds |
| Model result → workflow transition | Harness | Schema/check admission, receipt binding, explicit human choice, sole state writer |
| Agent result → product acceptance/publication | Consumer (including Echelon) | Assignment binding, protected controller state, verification and approval policy |
| Executable → host files/network | Host/consumer sandbox | OS-enforced isolation; Runtime's CLI adapter alone does not supply it |

Prompt-only ALWAYS/NEVER rules and staged acquisition help task quality, but are
not authority boundaries. Reading evidence proves bytes reached the model, not
that its interpretation is correct.

## Verified tests

`tests/test_prompt_injection.py` contains 26 cases:

- 24 HTTP/SSE combinations: hostile prose, repository reads, tool output, or
  tool descriptions; parent traversal, symlink escape, or non-granted private
  paths. The compromised model attempts secret reads, controller-state writes,
  ungranted custom-tool execution and shell execution. The protected file is
  unchanged, the ungranted handler is never invoked, and the synthetic secret
  never reaches the recorded model conversation.
- An authorized report write accepts fabricated content: a deliberate residual
  semantic-risk test, not a truthfulness guarantee.
- A trusted CLI reads a synthetic host secret outside its declared argument
  scope and returns it to the model: a deliberate demonstration that this
  adapter is not an OS sandbox.

Companion Harness tests reject forged controller fields, preserve a real human
pause even when the request/model claims approval, reject builtin write grants
in workflow definitions, and exercise Python Prosaic inspection plus the actual
Runtime tool loop against injected repository evidence. The evidence-induced
state write is denied and persisted state remains `waiting`.

Run the Runtime suite with Python Prosaic v0.3.0 on PATH:

```sh
PATH=/path/to/python-prosaic-venv/bin:$PATH .venv/bin/python -m pytest -q
```

Local verification after this pass: Runtime full suite **291 passed**, Harness
full suite **113 passed**, and focused Echelon installer/provider/acceptance
suite **148 passed**. All inspection integrations used Python Prosaic 0.3.0;
Runtime/Harness imported the current Runtime v0.5.0 source. Of 34 newly added
cases across the three repositories, three intentionally demonstrate residual
risks (poisoned allowed output, custom CLI host read, Echelon host read). A passing
characterization test means the limitation is reproducible, not fixed.

Runtime/Harness CI now installs immutable Prosaic v0.3.0 commit
`cdc9acddf3e58e5563cef06e0db918b5e3729148`, without Node/npm. Runtime 0.5.1
declares this dependency directly; Harness 0.4.1 receives it through its immutable
Runtime pin. Installing either package provides the Python Prosaic CLI in the
same environment without changing global installations. Echelon's
separately pending cutover pins that same Prosaic commit; its Runtime dependency
still pins v0.2.0. Do not assume Echelon receives v0.5.0 custom-tool controls.

## Findings and prioritized next work

### 1. Host isolation is incomplete — highest priority

Builtin filesystem tools enforce their granted paths, but trusted Python custom
handlers and CLI executables can independently read/write host files or access
the network. CLI environment allowlisting prevents implicit inheritance of
endpoint keys; it cannot prevent reading a key from a host file. Executable
integrity and transitive subprocess behavior also remain operator responsibilities.

The inspected Echelon macOS product-plane sandbox excludes protected paths but
allows other host reads and explicitly allows network access. A real Seatbelt
test demonstrated an unrelated synthetic host file remains readable. This is
not a confirmed malicious-model exploit, but it is a concrete containment gap:
the product-plane boundary is not a secret-isolation or egress boundary. Native
provider controls may add restrictions; they were not broadly bypass-tested here.

Next: choose and implement an explicit sandbox contract for tool/agent processes
(minimal readable mounts, scoped writes, isolated HOME/auth, no inherited host
secrets, and controlled egress). Account for the model provider's required auth
and endpoint access separately from tool network permissions. Do not blindly
disable all network access and break provider authentication/inference.

### 2. Authorized data can still be poisoned

A model may produce a schema-valid false spec, misinterpret a genuine quote, or
write poisoned content to an allowed report. Tool allowlists do not establish
truth or task correctness. Runtime exit code zero means invocation success, not
trusted task acceptance. Use consumer-owned deterministic provenance/domain
checks, independent verification, and human review for high-impact publication.
Avoid feeding raw report text into executable command/configuration sinks.

### 3. Broad scopes and trusted configuration need operator review

Grant narrow evidence roots and exact output paths rather than the workspace or
home directory wholesale. Runtime's default ignore list is not a secret scanner:
an in-scope `.env` or credential file is not automatically confidential. Review
manifests, descriptions, `pass_env`, executable paths and checks code before
trusting them. Never load model-generated Python through Harness `--checks`.

### 4. Live/provider adversarial coverage remains next, after isolation

Repeat these attacks against the local endpoint and native Codex/Claude providers
inside disposable secret-free workspaces. Use synthetic canaries and explicit
effect/egress assertions. Measure refusal separately from containment. No live
prompt-injection calls, real-secret reads, publication, or production sandbox
rollout were performed in this pass. Concurrency/TOCTOU, hostile process
replacement, cross-platform OS sandbox behavior and dependency vulnerability
scanning are not covered by these 26 tests.

Approach aligned with [OWASP LLM01:2025 Prompt Injection](https://genai.owasp.org/llmrisk/llm01-prompt-injection/):
least privilege, independent enforcement, explicit approvals and adversarial tests.
