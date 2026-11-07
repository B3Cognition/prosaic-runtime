# Runtime 0.9.16 — per-invocation OpenAI-compatible headers

Hosts may call `runtime.run(agent, request_headers={'x-tokenproxy-no-cache': '1'})`
or `runtime.turn(agent, message='...', request_headers={...})`, including execution
of a previously prepared turn. A private snapshot is taken before host callbacks;
every provider request in that invocation (including tool continuations and rule
repair) uses it. Concurrent and later invocations do not inherit headers.

Headers are transport options owned by the host, never model input. Names must
be ASCII HTTP tokens (up to128 characters), case-insensitive unique; values must
be printable ASCII (up to2048 characters). At most32 headers and8192 encoded
name/value/framing bytes are accepted. Runtime-owned credentials, cookies,
framing/proxy and correlation headers are refused. Invalid options raise a fixed
ValueError without echoing values before callbacks or HTTP. Nonempty options
require an OpenAI-compatible endpoint; unsupported entry points/providers do not
silently discard them. An empty mapping/None retains previous behavior.

The current API is `ProsaicRuntime.run`/`turn`; native CLI and unified pool APIs do
not accept this option. Header values are not added to prompts, result metadata,
accounting intents/observations or persistent identities. Normal JSON request
bodies, token/request/tool/deadline budgets and no-redirect protection are unchanged.
The provider sees these headers and can act on them; Runtime cannot certify an
endpoint's cache behavior. Never pass credentials in custom headers.

**Identity impact:** artifact digests, workflow fingerprints, prepared-input
identity and rule fingerprints gain no fields. Guard versions are unchanged;
existing compatible bindings do not need header-specific readmission. Hosts must
supply desired headers afresh on every invocation/resume; they are not persisted.

Core remains0.5.0; PostgreSQL recorder0.3.0 is an unchanged compatibility witness,
not republished. Qualification covers source and installed wheels, real synthetic
loopback transport, LinuxAMD64/ARM64, macOS, PostgreSQL16/18 and dependency floors.
No live provider certification or inference is implied. Publish only original
qualified merged-source bytes and verify public downloads and fresh consumers.

See [Cerebro requests](cerebro-requests.md) for the separate Harness validator V1
and deferred work, including workflow enforcement tags, accounting labels,
per-request effort evidence, explicit repair allowance and identity documentation.
