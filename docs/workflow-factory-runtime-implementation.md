# Runtime workflow factory prerequisite

Implemented Task 2 of the validated workflow factory plan on
`codex/validated-workflow-factory`, without dependency-pin, commit or packaging
changes.

`prosaic_runtime.validate_execution_artifact(artifact, config, *, acquisition=None)`
is a pure, ValueError-based admission API for inspected artifacts. It reuses the
existing inspection structure and tool declaration contract, validates supported
effort and the selected final route/default, and checks known Anthropic controls
after Runtime's forced feature overrides. Acquisition tools cannot broaden final
prose tools; supplied tier/effort must match, while omitted metadata inherits the
final execution context. Inputs are not modified. Runtime execution calls the
same helper, and the provider backend shares its Anthropic control checks.

`validate_custom_tools` and `custom_descriptors` are now public package exports.
They retain existing registry snapshots, independent descriptors and callback
identity without invoking handlers or authorizers.

The helper does not construct Runtime, discover CLI manifests, inspect paths,
render prompts, invoke callbacks or contact providers. Prosaic/Harness retain
canonical required-field admission; registration/grants and invocation-dependent
input/conversation limits remain separate. This API cannot certify capabilities
of a remote endpoint.

## Verification

Commands used the existing Runtime virtual environment on Python 3.11.15. HTTP
fixtures bind only to loopback and use synthetic responses; no live provider was
used. The filesystem sandbox denied loopback binding, so fixture-bearing runs
used test-only escalation.

The ten sandbox failures were the six status cases of
`test_doctor_discovery_auth_and_redaction` and four redirect cases of
`test_inference_does_not_forward_credentials_to_redirect_destination`; the other
267 errors occurred while setting up fixture servers. Both complete escalated
runs passed these cases.

| Stage | Command | Result |
| --- | --- | --- |
| Initial sandbox baseline | `.venv/bin/python -m pytest` | 179 passed, 1 skipped, 10 failed, 267 setup errors; loopback binding denied |
| Baseline before production changes | `.venv/bin/python -m pytest` | 456 passed, 1 skipped in 164.48s |
| Focused RED before production changes | `.venv/bin/python -m pytest tests/test_artifact_admission.py -q --tb=short` | 38 failed, 1 passed in 0.93s: missing public helpers and provider rejection occurring after `started` |
| Focused GREEN | Same focused command | 39 passed in 0.60s |
| Full GREEN | `.venv/bin/python -m pytest` | 495 passed, 1 skipped in 166.02s |
| Diff integrity | `git diff --check` | Passed |

The focused tests cover missing CLI directories, malformed typed artifacts,
unsupported tools/effort/routes, selected-provider controls, disabled controls and
forced feature overrides, acquisition inheritance/conflicts, unchanged inputs,
registry descriptor independence, rejection before execution starts, and local
HTTP acquisition on the final selected route. The full suite retains released
provider, acquisition, CLI, native-tool, accounting, transport and usage behavior.
