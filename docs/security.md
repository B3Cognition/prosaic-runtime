# Prompt-injection containment: first audit, 2026-10-01

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
