# Runtime 0.9.21 — parallel read tools

Opt into bounded concurrency for host-registered, independent read tools in API
runs and turns on OpenAI-compatible and Anthropic providers. Existing callers
keep sequential execution. Unmarked tools act as serial barriers.

- Admit the tool batch before dispatch and run eligible reads in bounded windows.
- Reduce guards, journal outcomes, evidence, and citations in provider call order.
- Report physical completion through the payload-free settlement event.
- Stop dispatch on cancellation and join callbacks already running. Handlers
  must cooperate with cancellation.

Parallel turns use encoding v5, frozen input v3, and evidence v4. Core 0.5.0
and Runtime recorder 0.3.0 retain their published bytes. See the
[configuration and compatibility guide](parallel-tools.md).
