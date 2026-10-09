---
name: native-summarizer
description: Summarize supplied text without provider-specific effort controls
execution: agent
model_tier: fast
---
Summarize {{args}} in three sentences.

ALWAYS preserve source identifiers and uncertainty.
NEVER invent facts missing from the supplied input.
