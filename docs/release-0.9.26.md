# Runtime 0.9.26 — constrained Nemotron lookup correction

Trusted hosts can pair `features.omlx_nemotron_tool_correction: true` on a
nonstreaming oMLX profile for
`NVIDIA-Nemotron-3.5-Lightning-30B-A3B-MLX-4bit` with
`correct_malformed_tool_calls=True` in Runtime or Harness.
See the [endpoint setup](conversations.md#constrained-nemotron-lookup-correction).

A schema-invalid singleton `lookup` can receive one correction constrained
to that native call when it is the only offered tool and accepts an empty
object. Runtime preserves a host-required first-tool choice and checks
the corrected call with the existing argument, authorization and budget
checks. Allow three provider requests for the original attempt, correction
and final answer. Other tool shapes keep ordinary correction.

Artifact digests, default prepared turns and receipt schemas are unchanged.
The opt-in enters the endpoint profile identity for readmission. Existing
Harness 0.9.19 bindings forward correction and preserve its receipt evidence.
Core 0.5.0 is the qualification baseline. PostgreSQL recorder 0.3.0 keeps its
published version and bytes.

The local live evaluation seeded four failures each in Runtime and Harness.
The unconstrained correction recovered zero of eight. The constrained
candidate recovered all eight, each with one authorized read and a grounded
answer. The fixtures cover one empty-argument tool and leave the natural
failure rate and argument-bearing tools unmeasured.
