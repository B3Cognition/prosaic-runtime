# Profiled and frozen context inputs

Harness owns named context packs, source selection, authorization and durable
workflow/conversation records. Runtime provides reusable model/private input
views, one-budget variant fitting and public frozen restoration. Runtime does
not retrieve files or database records on behalf of a pack.

## Separate model evidence from private checks

```python
from prosaic_runtime import Guard, RunPolicy, Verdict

def check_facts(stage, context):
    model = context.input_data
    complete = context.authoritative_input_data
    return Verdict(check_answer(context.draft, model, complete))

guard = Guard("complete-facts", "1", ("final",), check_facts,
              input_access="authoritativeAndModel")
prepared = runtime.prepare_turn(
    agent, message=user_message,
    input_data=selected_evidence, input_profile="my-evidence-v1",
    authoritative_input_data=complete_facts,
    authoritative_input_profile="my-private-facts-v1",
    policy=RunPolicy(max_provider_requests=2, max_tool_calls=0),
)
```

Register `guard` in the Runtime constructor and bind its enforcement tag in the
agent. Private guards must use `on_violation=fail`, cannot fix values, and cannot
echo refused values. Private reasons and callback exceptions become closed safe
failures. The model view remains `context.input_data`; private access is explicit.

`input_access="model"` excludes the private property. `shared` is the unchanged
legacy registration default. A turn with private input refuses shared bindings;
audit and explicitly select a role rather than silently changing what a check
sees. Generic guard factories, including `generic_guards`, accept `input_access`.
Use `model` for guards that check already disclosed tool output or formatting.

An explicit profile uses `conversation-turn-v3` and evidence v2. `inputViews`
binds both profile/digest pairs, guard access declarations and an identity for
the pair. Private facts remain outside provider payloads and tool arguments.
The complete prepared-turn identity also binds the exact current message,
history, policy, tool/guard descriptors, limits and native payload identity.
Default private bounds are 2 MiB, depth 32 and 65,536 JSON nodes.

## Fit evidence variants with history

```python
prepared = runtime.prepare_turn_variants(
    agent, message=user_message, history=history,
    input_data_variants=[rich_evidence, smaller_evidence, mandatory_evidence],
    input_profile="my-evidence-v1",
    context_policy=context_policy, token_counters=qualified_counters,
    policy=run_policy, limits=turn_limits,
)
selection = prepared.input_variant_selection.to_dict()
selected = prepared.selected_input_data
```

Candidates are ordered richest to mandatory. Runtime first fits mandatory
evidence with history, fixes that retained suffix, then considers richer roots.
All checks and final readmission use one assembly work budget. Insufficient work
for optional enrichment retains the admitted mandatory selection and reports
`selectionWorkLimited`; it does not silently obtain another budget.

Counters require explicit `TokenCounterDescriptor`, implementation/version,
tokenizer/template asset digests, provider/model/framing and guarantee. Synthetic
counter fixtures do not qualify a production tokenizer. Runtime has no automatic
generic tokenizer. Harness's byte-only path prepares the mandatory candidate
without claiming token counts.

## Export and restore the selected input

```python
frozen = prepared.export_frozen_input()
# Persist frozen.to_dict(), selection, current work and dispatch state first.
restored = runtime.restore_turn(
    agent, history=history, frozen_input=frozen,
    policy=run_policy, limits=turn_limits,
    context_policy=context_policy, token_counters=qualified_counters,
    context_work=saved_work,
)
result = runtime.turn(
    restored, token_counters=qualified_counters,
    context_checkpoint=persist_work_before_callback,
    on_admitted=persist_dispatch_marker,
)
```

Restoration admits the exact saved selection without rendering a new candidate,
counting tokens, running guards or recapturing sources. It checks the current
artifact, endpoint, limits, workspace, history and actual tool/guard/counter
registrations. Execution durably reserves work before native reconstruction or
counting. Checkpoint failure prevents the corresponding callback/effect.

The embedding controller owns authorization, leases, immutable receipts and
unknown-effect recovery. Restore is not permission to redispatch a completed or
uncertain call. An explicitly acknowledged new attempt may use
`fresh_attempt=True`; it advances the epoch while retaining initial preparation
charges. Byte-only epochs remain byte-only. Ordinary restoration cannot erase
an unknown work reservation or replenish an allowance.

`FrozenTurnInput` is a bounded closed portable value. It includes the exact
message, selected input, view identities, source/selected history digests,
selection/report, descriptor bindings, limits, work and execution epoch. Its
default whole-record ceiling is 8 MiB. The host must reserve actual retained
copies and completion evidence before effects; field maxima alone do not prove
an aggregate receipt will fit.

## Compatibility

`guard_input_views_v1`, `frozen_turn_inputs_v1` and `context_input_variants_v1`
advertise these APIs on `ProsaicRuntime`. CLI/native adapters lacking them refuse
the request. Unprofiled `run`/`prepare_turn`, conversation v1/v2, shared guard
material and existing rule fingerprints preserve their released behavior.
Core 0.5.0 and recorder 0.3.0 remain unchanged.

Run `python -I scripts/context_pack_wheel_smoke.py` with exact installed wheels
from outside the source tree. It checks both wire providers, frozen payload
identity, one-budget selection, private disclosure and byte-only epochs. Synthetic
acceptance does not establish real-model answer quality or cost savings.
