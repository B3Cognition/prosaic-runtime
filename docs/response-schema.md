# Opt-in schema-constrained generation

The local `0.9.8+jsonschema.3` candidate includes `json_schema_v1` in Runtime's
capabilities. This is unpublished and does not change released SDK pins.

```python
result = runtime.run(agent, arguments, response_schema={
    'type': 'object',
    'properties': {'addresses': {'type': 'array', 'items': {'type': 'integer'}}},
    'required': ['addresses'],
    'additionalProperties': False,
})
```

The host supplies the schema; prose and model output cannot enable it. Runtime
owns and validates an object JSON Schema before callbacks or dispatch, bounded
to 65,536 UTF-8 bytes, depth64 and32,768 JSON nodes. References must be local
fragments. The schema's actual encoded wire bytes also share `max_input_bytes`.
Runtime sends OpenAI-compatible `response_format.type=json_schema`, schema name
`response`, and `strict=true`. An explicit schema takes precedence over the
endpoint's legacy `json_mode`; calls that omit it retain their previous wire
format. No endpoint or shared configuration is mutated.

This first interface supports tool-free `run()` calls on OpenAI-compatible
providers. Tools, acquisition and native Anthropic fail before HTTP.
Conversation `turn()` does not accept this option. Unsupported HTTP responses
remain failures; Runtime never retries without the schema.

Trusted `RuntimeAdapter` hooks can call `execute(projected_arguments,
response_schema=schema, ...)` while retaining the existing reduced grants and
shared attempt budgets. If an outer caller supplied a schema, the hook inherits
it and cannot replace it with another schema. Each attempt records its schema
SHA-256, alongside existing arguments/usage evidence; the result records
`response_schema_v1` with schema SHA-256 and requested strictness.

This metadata records the request, not a proof of server enforcement. Providers
can ignore structured-output settings or use best-effort fallbacks. Inspect
provider evidence when qualifying an endpoint. Runtime preserves raw text and
usage; consumers must still parse strictly and validate the schema, citations,
permissions and domain semantics. It performs no fence stripping, JSON repair,
acceptance override or extra model call.

## Declared property order

`response_schema_order='declared'` explicitly preserves the host's nested JSON
object insertion order during schema ownership and transport. The default
`'canonical'` retains sorted keys and existing behavior. This can affect the
generation sequence on grammar-constrained servers; it changes neither JSON
Schema semantics nor consumer validation. It requires a schema and the new
`json_schema_order_v1` capability. Invalid modes fail before dispatch.

Adapters inherit an outer mode and cannot replace its schema or declared order.
`response_schema_v1` keeps its canonical digest. Declared-order calls additionally
record `response_schema_order_v1` (`mode`, `wire_schema_sha256`) on the result and
each adapter attempt. The wire digest hashes compact finite UTF-8 JSON without
sorting keys. Consumers must freeze this order-sensitive identity and a versioned
adapter/profile in saved workflows; canonical schema hashing alone cannot detect
an ordering change. All schema/input limits and raw output handling still apply.
