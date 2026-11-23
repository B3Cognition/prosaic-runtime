# Runtime 0.9.28

Guarded workflow steps with bounded tool recovery now retain the registered tool
version on both provisional start and completion events. Harness can match the
events to the tool registration it sealed when it admitted the workflow. This
fixes valid tool calls being followed by `tool_recovery_blocked`.

The regression exercises both OpenAI-compatible and Anthropic protocols. This
patch preserves the legacy event shape under the default policy and the existing
workflow and turn identities. Hosts can use it with Harness 0.9.20 and the
existing PostgreSQL recorder 0.3.0. For CLI sandbox configuration, see the
[0.9.27 upgrade notes](release-0.9.27.md).
