# Host-Registered Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute explicitly permitted host-registered Python tools through neutral Prosaic prose, with runnable Runtime and Harness catalogue examples.

**Architecture:** Runtime owns immutable tool definitions, validation, bounded dispatch and structural execution events. Harness receives the trusted registry through Python embedding, binds descriptors into workflow identity, and validates required successful execution before accepting output. YAML and prose select names; they never import implementations.

**Tech Stack:** Python 3.11+, existing OpenAI-compatible HTTP/SSE transport, Prosaic inspection, PyYAML, Draft 2020-12 validation through jsonschema, pytest.

**Spec:** [Approved stage-1 design](../specs/2026-10-01-custom-tools-security-design.md). This plan implements the custom-tool stage only, including its minimum safety contract; the broader adversarial campaign and MCP evaluation are not execution tasks here.

## Global Constraints

- Workspaces: `/Users/michalbachorik/work/prosaic-runtime` and `/Users/michalbachorik/work/prosaic-harness`; no Echelon changes or imports.
- Runtime branch: `codex/custom-tools-security`. Create the same branch in Harness before implementation, preserving any newly discovered local changes.
- Published Runtime 0.4.0 and Harness 0.3.0 stay immutable; no push, tag, release or released dependency-pin change.
- Host-registered callables only; no YAML/prose module loading, shell execution, recursive agent dispatch or MCP dependencies.
- `CustomTool.name`: ASCII `[A-Za-z][A-Za-z0-9_-]{0,63}`; reject built-in collisions and registry-key mismatch.
- Description at most 4 KiB UTF-8; nonempty version at most 128 UTF-8 bytes.
- Parameters: Draft 2020-12 object schema with `additionalProperties: false`; reject `$ref` and `$dynamicRef` throughout the schema.
- Defaults: arguments at most 16 KiB, result payload at most 64 KiB; strict finite JSON, no duplicate keys, at most 64 container levels.
- No-tools defaults and built-in alias expansion remain unchanged; all custom execution needs registration plus prose/configuration/host grants.
- Callbacks are trusted synchronous in-process code, not a sandbox. Deadline/cancellation checks occur before and after invocation; no hard preemption or exactly-once claim.
- Events/progress omit custom arguments, results, captured data and arbitrary exception text; no forged read receipts or human approvals.
- Preserve existing fingerprint structure for workflows without custom tools; checkpoint format stays version 2.
- Write a failing regression first for each new behavior, verify the focused test and both full suites before another fix. No lint configuration is added.
- Prosaic CLI must be on PATH. Live requests need explicit opt-in; keep keys and run receipts ignored.

## Review Focus

1. An authorization callback mutates its input: the handler must still receive the original validated arguments (Task 1).
2. The original schema/registry or a returned descriptor view is mutated after registration: execution identity and grants must remain unchanged or fail before dispatch (Tasks 1–2).
3. A valid custom call is followed by a denied built-in filesystem call in the same model turn: only the custom handler executes, and existing path protections stay intact (Task 2).
4. A successful event names the right tool but has the wrong version: Harness must reject it and cannot advance to human waiting (Task 4).
5. An unrelated tool registration changes between run and resume: the existing run stays resumable, while a required tool version change blocks (Task 4).

## Task 1: Immutable definitions and validated callback execution

**Files:**
- Create Runtime `src/prosaic_runtime/tools.py`, `tests/test_custom_tools.py`.
- Modify Runtime `src/prosaic_runtime/__init__.py`, `pyproject.toml`.

**Interfaces:**
- Produce exported `CustomTool(name: str, description: str, parameters: dict, handler: Callable[[dict], object], version: str, *, max_argument_bytes: int = 16384, max_result_bytes: int = 65536, authorize: Callable[[dict], bool] | None = None)`.
- `CustomTool.parameters` returns a defensive schema copy; `CustomTool.descriptor` returns JSON-safe name/description/parameters/version/bounds/authorization-presence metadata, excluding executable objects.
- Produce internal `validate_custom_tools(tools: Mapping[str, CustomTool] | None) -> dict[str, CustomTool]` and `custom_descriptors(tools: Mapping[str, CustomTool]) -> dict[str, dict]`.
- Produce internal `execute_custom_tool(tool: CustomTool, raw_arguments: str, *, check_boundary: Callable[[], None]) -> dict` and `ToolDeadlineExceeded(TimeoutError)` for the dispatch task.

- [ ] Write definition tests with a concrete schema and real callbacks:

```python
SCHEMA = {'type': 'object', 'required': ['sku'],
          'properties': {'sku': {'type': 'string', 'pattern': '^SKU-[0-9]{3}$'}},
          'additionalProperties': False}

def test_schema_snapshot_is_not_changed_by_caller():
    schema = deepcopy(SCHEMA)
    tool = CustomTool('lookup_catalog', 'Lookup a synthetic item', schema,
                      lambda args: args['sku'], 'catalog-v1')
    schema['properties']['sku']['pattern'] = '.*'
    view = tool.parameters
    view['properties']['sku']['pattern'] = '.*'
    assert tool.parameters['properties']['sku']['pattern'] == '^SKU-[0-9]{3}$'

def test_authorizer_cannot_change_validated_handler_arguments():
    seen = []
    def authorize(args):
        args['sku'] = 'SKU-999'
        return True
    tool = CustomTool('lookup_catalog', 'Lookup', SCHEMA,
                      lambda args: seen.append(args) or {'found': True},
                      'v1', authorize=authorize)
    result = execute_custom_tool(tool, '{"sku":"SKU-001"}', check_boundary=lambda: None)
    assert result['status'] == 'ok'
    assert seen == [{'sku': 'SKU-001'}]
```

- [ ] Run `.venv/bin/python -m pytest -q tests/test_custom_tools.py`; confirm feature tests fail because the definition/dispatcher does not exist. After creating the importable interface, rerun to confirm behavioral failures rather than relying only on import errors.
- [ ] Implement definition validation and immutable schema snapshots using canonical finite JSON stored privately; return fresh decoded schema/descriptor copies. Validate exact integer bounds, callable handler/authorizer, bounded names/text, closed object schemas and forbidden references. Add `jsonschema>=4.23` to Runtime dependencies and export `CustomTool`.
- [ ] Pin registration guards with tests rejecting built-in names, invalid/oversized names, registry-key mismatch, missing/noncallable handlers, empty/oversized versions, boolean/zero/negative limits, open/non-object/invalid schemas and embedded references. Each rejection must occur before an endpoint request.
- [ ] Implement strict argument parsing using `object_pairs_hook` duplicate rejection and `parse_constant` rejection; enforce the nesting limit before schema validation. Call `Draft202012Validator(tool.parameters).validate(arguments)` before authorizing or invoking. Pass separate deep copies to authorizer and handler.

The callback sequence is fixed. Keep parsing and serialization in focused
helpers in `tools.py`: `parse_arguments(raw, schema, maximum) -> dict`,
`bounded_result(value, maximum) -> dict`, and `depth(value) -> int`. The parser
rejects raw JSON constants/duplicate keys before applying schema validation:

```python
def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result

def reject_constant(value):
    raise ValueError('nonfinite number')

def depth(value):
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError('JSON object keys must be strings')
        return 1 + max((depth(child) for child in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((depth(child) for child in value), default=0)
    if value is None or type(value) in (str, bool, int, float):
        return 0
    raise ValueError('not a JSON value')

def parse_arguments(raw, schema, maximum):
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > maximum:
        raise ValueError('argument limit')
    arguments = json.loads(raw, object_pairs_hook=unique_pairs,
                           parse_constant=reject_constant)
    if not isinstance(arguments, dict) or depth(arguments) > 64:
        raise ValueError('argument shape')
    Draft202012Validator(schema).validate(arguments)
    json.dumps(arguments, allow_nan=False)  # Also reject float overflow such as 1e999.
    return arguments

def bounded_result(value, maximum):
    try:
        if depth(value) > 64:
            return {'status': 'error', 'error': 'invalid_result'}
        envelope = {'status': 'ok', 'result': value}
        text = json.dumps(envelope, allow_nan=False)
        if len(text.encode('utf-8')) > maximum:
            return {'status': 'error', 'error': 'result_limit'}
        return json.loads(text)
    except (TypeError, ValueError, RecursionError, OverflowError):
        return {'status': 'error', 'error': 'invalid_result'}
```

Capture errors before callbacks, then preserve real cancellation/deadline exceptions:

```python
check_boundary()
try:
    arguments = parse_arguments(raw_arguments, tool.parameters, tool.max_argument_bytes)
except (TypeError, ValueError, RecursionError, OverflowError, ValidationError):
    return {'status': 'error', 'error': 'invalid_arguments'}
if tool.authorize is not None:
    try:
        allowed = tool.authorize(deepcopy(arguments)) is True
    except (Cancelled, ToolDeadlineExceeded):
        raise
    except Exception:
        allowed = False
    if not allowed:
        return {'status': 'error', 'error': 'authorization_denied'}
check_boundary()
try:
    value = tool.handler(deepcopy(arguments))
except (Cancelled, ToolDeadlineExceeded):
    raise
except Exception:
    return {'status': 'error', 'error': 'handler_error'}
check_boundary()
return bounded_result(value, tool.max_result_bytes)
```

Implement registration guards and every parser/serializer branch before exposing
the dispatcher. Check code snippets against the failing tests, including cycle
and nesting boundaries. Use stable error dictionaries; never serialize exception messages.
The success-envelope byte limit does not truncate an error into invalid JSON:
fixed error envelopes are independently bounded to 256 bytes even when a host
configures a smaller successful-result limit. Document that distinction.

- [ ] Add parameterized tests for malformed JSON, duplicate keys, NaN/infinity, arrays/scalars, unknown/missing fields, wrong types, invalid SKU, denied/throwing/non-boolean authorizers, handler exceptions with a synthetic secret, invalid/deep/oversized results and before/after boundary failure. Assert handler call counts and captured arguments, not mocked validator behavior.
- [ ] Run focused tests and both full suites with Prosaic on PATH. Verify existing tests still pass; commit only Task 1 files as `Add immutable host custom-tool definitions and validated execution`.

## Task 2: Bounded Runtime integration and native HTTP/SSE execution

**Files:**
- Modify Runtime `src/prosaic_runtime/runtime.py`, `src/prosaic_runtime/openai_compatible.py`.
- Create Runtime `src/prosaic_runtime/tool_registry.py`, `tests/test_custom_tool_transport.py`.

**Interfaces:**
- Consume Task 1's `CustomTool`, `validate_custom_tools`, `custom_descriptors`, `execute_custom_tool`, `ToolDeadlineExceeded`.
- Extend `ProsaicRuntime.__init__(config, *, source='.prosaic', executable='prosaic', custom_tools=None)`; expose `tool_descriptors` as a read-only mapping of defensive descriptor copies; advertise `custom_tools_v1`.
- Internal `BoundedToolRegistry(builtin_registry, custom_tools, allowed_tools, check_boundary)` implements existing `openai_tools() -> list[dict]` and `execute_message(tool_call: dict) -> dict` contracts.
- Backend hooks `tool_call_summary(tool_call: dict) -> str` and `tool_event_metadata(name: str) -> dict` retain default summaries/empty metadata; bounded Runtime overrides suppress custom argument previews and supply `tool_version`.

- [ ] Reuse the real local server/completion fixtures from `tests/test_runtime.py`, not a mocked backend. Add this core transport test:

```python
def test_custom_tool_execution_is_native_and_versioned(server, tmp_path):
    url, requests, responses = server
    seen, events = [], []
    tool = CustomTool('lookup_catalog', 'Lookup', SCHEMA,
                      lambda args: seen.append(args) or {'found': True}, 'v1')
    config = RuntimeConfig({'local': EndpointConfig(url, 'test', features={'streaming': False})},
                           {'fast': 'local'}, 'local', frozenset({'lookup_catalog'}))
    responses.extend([completion('', [{'id': 'lookup-1', 'type': 'function', 'function': {
        'name': 'lookup_catalog', 'arguments': '{"sku":"SKU-001"}'}}]), completion('done')])
    result = ProsaicRuntime(config, custom_tools={'lookup_catalog': tool}).run(
        artifact(['lookup_catalog']), cwd=tmp_path,
        policy=RunPolicy(allowed_tools=frozenset({'lookup_catalog'})), on_event=events.append)
    assert result.exit_code == 0 and result.token_usage == 14
    assert seen == [{'sku': 'SKU-001'}]
    assert requests[0]['tools'][0]['function']['name'] == 'lookup_catalog'
    message = next(m for m in requests[1]['messages'] if m['role'] == 'tool')
    assert json.loads(message['content']) == {'status': 'ok', 'result': {'found': True}}
    assert any(e['event'] == 'tool_completed' and e['name'] == 'lookup_catalog'
               and e['status'] == 'ok' and e['tool_version'] == 'v1' for e in events)
```

- [ ] Run the transport test and observe the expected missing constructor support failure.
- [ ] Snapshot and validate registration in Runtime's constructor. Accept requested names only from built-ins plus registered names; compute the existing prose/configuration/policy intersection without widening aliases. Keep callback objects out of Invocation metadata.
- [ ] Extend the bounded backend constructor with the trusted registry and absolute invocation deadline. Its `make_registry` composes the existing built-in registry with `BoundedToolRegistry`. Boundary checks call `check_cancelled()` and raise `ToolDeadlineExceeded` on deadline expiry; Runtime converts that exception into a timed-out Result preserving known usage.
- [ ] Implement custom advertisements and result messages without private registry inheritance:

```python
# openai_tools(): preserve builtin_registry.openai_tools(), then append granted definitions.
{'type': 'function', 'function': {
    'name': tool.name, 'description': tool.description, 'parameters': tool.parameters}}

# execute_message(): validate the function/name shape and grant before selecting a callback.
# Registered custom name: execute_custom_tool(...); built-in name: delegate unchanged.
{'role': 'tool', 'tool_call_id': call_id,
 'content': json.dumps(payload, allow_nan=False)}
```

- [ ] Route both current `_tool_call_summary` call sites through the backend hook, including tool-limit failure metadata. Add only host-owned `tool_version` to custom completion events; keep name/status/read receipts owned by existing dispatch code. Custom results cannot satisfy built-in read receipt logic.
- [ ] Repeat the transport test with actual SSE tool deltas and a usage footer. Add grant-omission cases independently for registration/prose/config/host; test no-tools default and `read` alias not advertising registered custom tools.
- [ ] Add real HTTP tests for mixed custom/denied built-in calls, strict input errors not invoking callbacks, custom acquisition ordering, explicit initial-tool mismatch before invocation, cancellation/deadline after callback, cross-invocation grants, and progress/event redaction using synthetic secret strings.
- [ ] Run both full suites, including existing redirect/usage/acquisition/path checks. Commit Task 2 files as `Execute host custom tools through the bounded native transport`.

## Task 3: Runnable Runtime catalogue example

**Files:**
- Create Runtime `examples/catalog_tools.py`, `examples/catalog.json`, `examples/custom-tools.yml`, `examples/run_custom_tool.py`, `examples/.prosaic/subagents/catalog-reader.md`, `tests/test_custom_tool_example.py`.
- Modify Runtime `examples/README.md`, `README.md`, `CHANGELOG.md`; ensure existing `MANIFEST.in` includes the new assets.

**Interfaces:**
- `catalog_tools.make_tools() -> dict[str, CustomTool]` snapshots a fixed adjacent catalogue and assigns a version including its byte SHA-256.
- `run_custom_tool.main() -> int`; CLI flags `--live`, `--config`, `--profile`, `--base-url`, `--model`, `--sku`, `--no-stream`.
- Fixed catalogue record for SKU-001: `{'sku': 'SKU-001', 'name': 'Demo Widget', 'price_cents': 1250, 'currency': 'USD'}`.

- [ ] Write a subprocess test invoking the checked-in program against the local server with a native lookup response and final JSON; assert stdout contains the final Result and successful versioned execution evidence. A separate subprocess test omitting `--live` must exit nonzero before any endpoint request.
- [ ] Run `.venv/bin/python -m pytest -q tests/test_custom_tool_example.py` and observe the missing example failure.
- [ ] Implement the fixed-data callback and registration:

```python
def lookup(args):
    item = catalog.get(args['sku'])
    return {'found': item is not None, 'item': deepcopy(item)}

tools = {'lookup_catalog': CustomTool('lookup_catalog', 'Look up a synthetic catalogue item.',
                                     SCHEMA, lookup, 'catalog-' + catalog_sha256)}
runtime = ProsaicRuntime(config, source=EXAMPLES / '.prosaic', custom_tools=tools)
result = runtime.run('subagents/catalog-reader.md', args.sku, cwd=EXAMPLES,
    policy=RunPolicy(allowed_tools=frozenset({'lookup_catalog'}), initial_tool='lookup_catalog',
                     timeout_s=config.limits.timeout_s,
                     max_tool_rounds=config.limits.max_tool_rounds), on_event=events.append)
```

The module constructs SCHEMA as in Task 1; load the fixed adjacent catalogue
with strict finite JSON and hash those exact bytes. There is no model-supplied
path, URL, command or credential argument. Profile overrides route `fast` to
the selected endpoint without changing the YAML tool grant. Check `--live`
before creating Runtime or starting requests.

- [ ] Add neutral prose with paired ALWAYS/NEVER rules and `tools: [lookup_catalog]`; require one exact JSON answer, report absent identifiers without inventing records. YAML grants only lookup_catalog and uses an environment variable for credentials.
- [ ] Test not-found lookup, malformed SKU, config denial, invocation from another current directory and Prosaic inspection. Document installation using the development checkout, registration versus permission, all three grants, and the no-sandbox limitation.
- [ ] Run both full suites and Runtime package build; verify catalogue, program and Markdown exist in the sdist and no local credentials/receipts are included. Commit as `Add a runnable host-registered catalogue tool example`.

## Task 4: Harness registry binding and required-tool admission

**Files:**
- Modify Harness `src/prosaic_harness/workflow.py`, `src/prosaic_harness/engine.py`.
- Create Harness `tests/test_custom_tools.py`.

**Interfaces:**
- Consume Runtime's `CustomTool`, `custom_tools_v1` capability and `tool_descriptors`.
- Extend `Workflow.load(path, *, executable='prosaic', custom_tools=None)`; add host-memory `Workflow.custom_tools` and a JSON-safe `Workflow.tool_descriptors` snapshot containing only registered custom names requested by agent prose.
- Default Harness forwards the workflow registry to Runtime. Injected Runtime descriptors must match required workflow descriptors before any dispatch.
- Extend current fingerprint conditionally with `custom_tools: workflow.tool_descriptors`; omit this key entirely when no custom names are requested.

- [ ] Create a real-Prosaic temporary workflow with the Task 3 catalogue prose, `tools: [lookup_catalog]`, `require_tools: [lookup_catalog]`, no filesystem roots, a closed output schema and a human pause. Use the local HTTP fixture to prove run→native lookup→schema admission→waiting→human reject without another model call.
- [ ] Run `.venv/bin/python -m pytest -q tests/test_custom_tools.py`; observe Workflow.load rejecting the new registration keyword.
- [ ] Snapshot registration through the public Runtime constructor, not imports of its private validators:

```python
registry = dict(custom_tools or {})
adapter = ProsaicRuntime(config, custom_tools=registry)
registered_descriptors = dict(adapter.tool_descriptors)
```

Extend step validation to built-in read names plus registered custom names,
preserving built-in write denial. Require read_roots only when the step grants
built-in reads; keep require_reads restricted to declared native read_file evidence.
- [ ] Bind descriptors for every custom name the inspected prose explicitly requests, even if the particular step does not grant that requested name. Ignore wholly unused registrations. Include optional descriptor data in both initial and current fingerprint calculations, and compare injected adapters before dispatch.
- [ ] Build successful required-tool evidence using the current event contract, with the version condition for custom names:

```python
successful = {
    e.get('name') for e in receipt['events']
    if e.get('event') == 'tool_completed' and e.get('status') == 'ok'
    and (e.get('name') not in self.workflow.tool_descriptors
         or e.get('tool_version') == self.workflow.tool_descriptors[e['name']]['version'])
}
missing = set(step.get('require_tools', [])) - successful
```

- [ ] Add deterministic tests for missing capability/adapter descriptor mismatch, missing registration, denied config/prose grant, wrong-version event, declared-but-unexecuted required custom tool, changed required version on resume, unchanged fingerprint when an unused registration changes, and unchanged legacy workflow fingerprints. Assert no HTTP/handler execution on preflight failure and no accepted output on failed admission.
- [ ] Install the local Runtime explicitly in the Harness development environment (`uv pip install --python .venv/bin/python --no-deps -e ../prosaic-runtime`; install its new jsonschema dependency as needed). Do not change Harness's published Git pin.
- [ ] Run both full suites. Commit Harness files as `Bind host custom tools into workflow execution and resume identity`.

## Task 5: Harness example, coordinated verification and handoff

**Files:**
- Create Harness `examples/catalog_tools.py`, `examples/catalog.json`, `examples/catalog-tool.yml`, `examples/custom-tools-runtime.yml`, `examples/catalog-checks.py`, `examples/schemas/catalog-result.json`, `examples/run_custom_tool.py`, `examples/.prosaic/subagents/catalog-reader.md`, `tests/test_custom_tool_example.py`.
- Modify Harness `examples/README.md`, `README.md`, `CHANGELOG.md`, `docs/verification.md`.
- Modify Runtime `CHANGELOG.md` with final stage-1 verification notes where needed.

**Interfaces:**
- Both repositories contain self-contained matching catalogue fixtures/tool modules, not sibling imports; each repo's contract tests assert the same expected descriptor and fixed SKU-001 answer without requiring a sibling checkout in CI.
- Harness `run_custom_tool.main() -> int`; flags `--live`, `--config`, `--run-dir`, `--sku`, `--resume`, `--choice`.
- `catalog-checks.py` exports CHECKS with a check returning an empty list only when the admitted answer exactly matches the fixed catalogue for the original request SKU.

- [ ] Write a real subprocess integration test that runs the shipped program, inspects the versioned receipt and waiting status, then resumes with reject and asserts no additional endpoint calls. Test a schema-valid invented record that reaches bounded rejection rather than admission.
- [ ] Run the example test; observe the absent assets/entrypoint failure.
- [ ] Implement embedding with the same registry on both run and resume:

```python
workflow = Workflow.load(EXAMPLES / 'catalog-tool.yml', custom_tools=make_tools())
if args.config:
    workflow.config = RuntimeConfig.load(args.config)
    workflow.fingerprint = workflow.current_fingerprint()
harness = Harness(workflow, args.run_dir,
                  validators=load_validators(EXAMPLES / 'catalog-checks.py'))
state = harness.resume(choice=args.choice) if args.resume else harness.run({'sku': args.sku})
```

The workflow's lookup step requires successful `lookup_catalog`, the catalogue
validator and the closed result schema before transitioning to a bound human
pause. Approve/reject finish without external effects. Require `--live` for a
new run and for resume that could contact the model; explain that conservative
rule even though an ordinary waiting human choice uses no inference.

- [ ] Write step-by-step commands for the development Runtime override, endpoint configuration, first run, receipt inspection and same-config resume. Note that normal released Harness lacks this capability until a separately authorized coordinated release.
- [ ] Run both full suites and both builds on the final code; use temporary output directories so released assets are not overwritten. Install development wheels explicitly into a fresh environment, verify import paths, then rerun suites with `-o pythonpath=''` and Prosaic on PATH.
- [ ] Validate both checked-in examples against deterministic local HTTP/SSE. When the supplied TokenProxy is reachable, run the lookup against each configured model with streaming on/off; preserve strict admission, record blocked cases and do not retry by changing mode. Source TOKENPROXY_KEY without printing it. Store receipts under ignored run directories.
- [ ] Record test counts, packaging checks, live outcomes, trusted-callback limitations and any remaining issue in verification documentation. Keep the broader injection campaign and MCP evaluation marked pending.
- [ ] Commit examples/docs/tests as `Demonstrate version-bound custom tools in Prosaic Harness`; check both worktrees for only intended changes, then report commits and executable commands. Do not push or release.

## Verification commands

Run the full suites from their own repository roots, with the companion Prosaic
CLI available:

```sh
PATH="/Users/michalbachorik/work/prosaic-harness/.venv/bin:$PATH" .venv/bin/python -m pytest -q
.venv/bin/python -m build --outdir /absolute/new/temporary/output
```

Use `mktemp -d /tmp/prosaic-custom-tools.XXXXXX` to obtain that new output root.
Use named output subdirectories for each repository. Fresh installed-wheel
checks disable pytest source paths with `-o pythonpath=''` and verify both
module `__file__` paths point to the temporary environment's `site-packages`.

## Plan self-review and execution gate

This plan covers every stage-1 spec section: definition, authority intersection,
strict callback validation, owned result envelopes, safe events, shared Runtime
limits/acquisition, Harness identity/admission, examples, documentation, packaging
and testing. It deliberately defers the wider prompt-injection adversarial
campaign and MCP research. The five Review Focus cases are assigned to Tasks 1,
2 and 4. No production implementation starts until the user reviews this plan
and selects the execution approach.
