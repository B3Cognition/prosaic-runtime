# Native Anthropic provider

## Intent and scope

Add native Anthropic Messages API execution to Prosaic Runtime while retaining
Runtime ownership of tool execution, permission grants, cancellation, deadlines,
acquisition validation, read receipts, and accounting. The user approved this
scope on 2026-10-09. Core Prosaic and Harness need no changes.

Version one supports text, streaming, builtin tools, host-registered tools, and
trusted CLI tools through the existing bounded loop. It does not implement
Claude Code, Bedrock, Vertex, image inputs, extended thinking, or provider-hosted
tools. No live paid calls are necessary for automated verification.

## Approach and alternatives

Use a shared execution loop with provider-specific transport adapters. Copying
the loop would duplicate security behavior; translating Anthropic responses into
fake OpenAI HTTP responses would hide native protocol and usage semantics.
Extract only the seams required by the second provider, avoiding a general
plugin framework or unrelated rewrites.

## Configuration and compatibility

Append a provider field to EndpointConfig, defaulting to `openai-compatible` so
existing YAML and positional Python construction retain their meanings. Accept
`anthropic` explicitly and reject unknown providers during configuration loading.
Keep model and base_url operator supplied; Anthropic example configuration uses
`https://api.anthropic.com/v1` and `ANTHROPIC_API_KEY`. Append `/messages` once to
the configured API base, following the existing base URL convention.

Keep existing public Result, Invocation, RunPolicy, event, and serialized ledger
contracts compatible. Preserve the low-level OpenAICompatibleBackend integration
seam used by Echelon, including its existing overridable hooks. Existing OpenAI
profiles must produce unchanged requests and behavior.

## Execution boundary

Introduce an internal provider-neutral turn representation and transport boundary
for sending conversation turns. A turn includes assistant text, tool requests,
provider continuation content, stop outcome, raw provider metadata, and usage.
Keep any OpenAI-shaped internal conversation representation behind explicit
conversion functions; it is not the new public provider contract.

The common loop retains grants, strict tool-name checks, initial-tool enforcement,
acquisition tools and final-prompt transitions, compaction, no-progress handling,
tool-round limits, event emission, and result assembly. Provider adapters cannot
execute tools or expand grants. All paths, including no-tool execution, pass
through the same bounded request, response, deadline, and accounting hooks.

Anthropic conversion separates system instructions from messages, maps function
schemas to input_schema, preserves tool_use IDs, and returns tool_result blocks
in user messages. Preserve complete assistant tool-use content for continuation;
do not reconstruct it from text alone. Forced first tools map to native tool
choice, while the shared loop still validates observed calls before execution.
Validate Anthropic max_tokens as required and positive. Reject explicitly enabled
unsupported protocol features before inference; do not silently omit them.

## Native transport and streaming

Use the existing Python HTTP facilities, redirect prohibition, key resolution,
and size limits. Send x-api-key and anthropic-version headers to `/messages`.
Parse both JSON Messages responses and native SSE events, assembling text and
incremental tool-input JSON by content-block index. Validate event ordering,
block identity, JSON object arguments, and complete terminal framing. Ignore ping
and safely ignorable metadata events; fail clearly for unsupported content blocks.

Text deltas remain provisional until successful completion. Map end_turn and
stop_sequence to successful completion, tool_use to continuation, and max_tokens
to incomplete generation. Refusal, pause_turn, malformed streams, unsupported
blocks, and stream error events return explicit failures. Never automatically
retry or execute incomplete tool calls. Retain the native stop reason in metadata.

## Usage and durable accounting

Select response observation semantics from the actual endpoint provider; never
apply OpenAI snapshot rules to Anthropic message_start/message_delta events.
Capture native message/model identifiers and normalize validated input/output
counts without summing cumulative output snapshots. Terminal completion requires
message_stop for streams and a valid complete response for JSON. Interrupted
streams retain partial evidence; contradictory evidence is untrusted.

Anthropic cache-read and cache-creation counts are separate from input_tokens.
Canonical input totals include ordinary input plus both cache categories;
cached_input_tokens represents cache reads. Preserve raw categories in result
metadata. Cache creation has distinct pricing semantics absent from the existing
rate-card schema: mark such accounting evidence unsupported for cost calculation
rather than price it as ordinary input. Nonzero unsupported provider usage
categories likewise cannot produce a trusted cost. Missing details remain unknown.

The configured recorder provider_id must match the selected provider when durable
accounting is enabled; mismatch fails before inference. Existing OpenAI recorder
defaults remain valid. No ledger schema or PostgreSQL migration is required;
unknown, partial, unsupported, and untrusted statuses retain existing semantics.

## Verification and documentation

Add local HTTP fixture tests for native headers/payloads, JSON and SSE text,
fragmented tool input, multi-tool continuation, forced first tools, acquisition,
custom tools, denied grants, malformed/incomplete streams, errors, cancellation,
redirects, request/response limits, and usage/accounting evidence. Exercise the
public Runtime API rather than only adapter conversion functions. Include mixed
provider profiles and regressions for existing OpenAI behavior and Echelon hooks.

Run the full Runtime pytest suite, build its wheel, and run its documented wheel
smoke verification. Document provider selection, API base conventions, credentials,
supported capabilities, and accounting limitations; add a runnable Anthropic
configuration example without secrets. A live Anthropic smoke test is optional
and requires available credentials plus explicit authorization for paid calls.

## Acceptance criteria

- Existing OpenAI configurations and integration hooks pass unchanged regressions.
- An Anthropic profile executes text and bounded tool tasks through native Messages.
- All existing Runtime grants and limits apply to both providers.
- Streaming failures cannot produce successful results or execute partial tools.
- Provider accounting records correct evidence or an explicit nonbillable status.
- Package installation works with no Node or TypeScript execution.

## Protocol references

- https://platform.claude.com/docs/en/build-with-claude/streaming
- https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons

Implementation must verify current Messages, tool-use, and usage documentation
against these requirements before writing provider-specific parsers.
