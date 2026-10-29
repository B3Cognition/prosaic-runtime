# Runtime 0.9.9

Adds public, policy-admitted model-free custom tool execution (`run_tool`,
`direct_tool_v1`) with shared execution budgets, authorizers, cancellation,
strict argument/result bounds and zero provider requests. Registration does not
grant execution permission. See [direct-tool.md](direct-tool.md).

Adds host-configured OpenAI-compatible completion token wire selection and
reasoning settings while retaining semantic caps and legacy profile defaults.
Unset temperature is omitted, including constrained requests. No endpoint or
model is selected implicitly.

Core remains the qualified 0.4.1 candidate. PostgreSQL adapter 0.3.0 is a
compatibility witness, not a new adapter publication. Source, installed wheel,
platform, database and dependency-floor gates must pass before publishing the
same candidate bytes under the new core version.
