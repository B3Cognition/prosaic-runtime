---
name: summarizer
description: Summarize supplied text
execution: agent
model_tier: fast
effort: low
---
Summarize {{args}} in three sentences.

ALWAYS preserve source identifiers and uncertainty.
NEVER invent facts missing from the supplied input.
