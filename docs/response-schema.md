# Opt-in schema-constrained generation

Check for `json_schema_v1` in Runtime's capabilities before sending a schema.

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
to 65,536 UTF-8 bytes, depth 64 and 32,768 JSON nodes. References must be local
fragments. The schema's actual encoded wire bytes also share `max_input_bytes`.
Runtime sends OpenAI-compatible `response_format.type=json_schema`, schema name
`response`, and `strict=true`. An explicit schema takes precedence over the
endpoint's legacy `json_mode`; calls that omit it retain their previous wire
format. No endpoint or shared configuration is mutated.

This first interface supports tool-free `run()` calls on OpenAI-compatible
providers. Tools, acquisition, native Anthropic and Runtime citation-policy
bindings fail before HTTP.
Conversation `turn()` does not accept this option. Unsupported HTTP responses
remain failures. A server warning that declares a strict-schema downgrade also fails the invocation before Runtime consumes the answer.

Trusted `RuntimeAdapter` hooks can call `execute(projected_arguments,
response_schema=schema, ...)` while retaining the existing reduced grants and
shared attempt budgets. If an outer caller supplied a schema, the hook inherits
it and cannot replace it with another schema. Each attempt records its schema
SHA-256, alongside existing arguments/usage evidence; the result records
`response_schema_v1` with schema SHA-256 and requested strictness.

The metadata identifies the requested schema. Qualify the server's enforcement
separately. Runtime returns raw text and usage. Consumers parse the response
strictly and validate its schema, citations, permissions and domain semantics.

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
