# Safe tool events and citations

Development contract: `tool_events_v1`, `tool_results_v1`, `citations_v1`.
These APIs require the new development SDK; Runtime 0.9.19 does not provide them.

Register `CustomTool(event_policy=ToolEventPolicyV1(...))` independently from
`result_contract=ToolResultContractV1(...)`. Event-only handlers retain their
existing return type. Typed handlers return `ToolResultV1(data=..., evidence=...)`.
An `EvidenceDraftV1` selects captured JSON data with a JSON Pointer. Runtime
computes its hash; source revisions are producer claims and may be absent.

Pass `ToolContractBindingsV1` explicitly to `run`, `turn`, or `run_tool`.
Bindings pin the invocation scope, versioned authorizers and optional
`CitationPolicyV1('optional' | 'required')`. Model loops need finite provider/tool
caps. Execution permission, evidence permission and viewer display permission
are separate grants; callbacks must return literal `True` and exceptions deny.

`tool_started` and `tool_completed` retain their names. Enabled events add
`toolEventV1` with stable Runtime identity, phase and an authorized scalar display.
Start follows validation, authorization and journal claim; replay emits completion
without a new start. Displays allow eight fields, 256 UTF-8 bytes per string and
1 KiB total, within a 4 KiB event. Missing/non-scalar/control-character values are
omitted; oversized displays are unavailable. General telemetry carries no display.

Evidence permission purposes are `modelUse`, `cite`, and `retainedRead`.
Private locators and declarations stay in invocation evidence; the model receives
data and compact references. Only the final encoded request and an actual HTTP
response establish Runtime-observed delivery. Compacted or unsent values do not
become citable. A changed post-tool selection loses its reference; completed
effects remain completed even if projection or answer validation later fails.

The reserved final-answer token is `[cite:<id>]`, including inside code and quotes.
After ordinary text fixes, every token must reference delivered evidence with a
current citation grant. Required mode needs a nonempty answer with a citation.
This establishes provenance, not semantic entailment. All citation-enabled draft
text is withheld until accepted; approved tool progress remains live. Repair uses
only an existing explicit answer-rule retry allowance and remaining budgets.
`Result.stdout` retains tokens; `citations_v1` contains the final answer hash,
registry digest and exclusive UTF-8 marker offsets (128 markers/32 KiB maximum).

Use `value_in_tool_output(tool_result_contract='v1')` to evaluate logical typed
data without treating wrapper versions or source metadata as domain facts. The
default guard retains its earlier version and behavior.

API OpenAI-compatible and Anthropic model loops support automatic citations.
Direct calls have zero provider exposure and refuse automatic citations.
Structured loops expose pending typed results and safe events. A host can submit
an explicit `HostDeliveryAttestationV1` to pure validation; the sidecar is labeled
`hostAttested` and never claims Runtime-observed delivery. Native CLI execution
and automatic terminal-tool citations refuse these unsupported combinations.

Enabled turns use `conversation-turn-v4`, frozen input version 2 and turn evidence
version 3. Restore requires matching bindings; old formats are neither upgraded
nor given citation authority. Feature-disabled bytes and journal outer frames
retain their existing formats. Evidence is capped at 64 KiB, with 64 declarations
per result and 256 registry entries; existing smaller input/output limits win.

Parallel tool execution, MCP clients and sub-agents remain separate follow-ups.

Trusted hosts adapt verified pack/retained receipts with `HostEvidenceImportV1`.
Bind the owned imports explicitly in `ToolContractBindingsV1.retained`; a matching
ID in history grants nothing. Each import pins the captured representation, scope,
origin receipt digest and exact public model container. Runtime rechecks current
model-use permission and actual submission of that container before eligibility.
The host verifies the original receipt and tenant/actor binding; decoding its
metadata alone never grants authority. Imported bodies stay outside the registry
sidecar, which holds bounded hashes/references to the existing captured context.
