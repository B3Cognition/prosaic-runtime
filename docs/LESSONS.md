# Lessons

## TOON tool results skipped evidence delivery checks (PR #42 review)

- What: with `tool_result_format: toon`, sent tool results were missing from evidence delivery tickets. Use was
  not authorized again before dispatch, delivery receipts were empty and valid required citations failed.
- Root cause: the request encoder turned tool results into TOON before `EvidenceRegistryV1.prepare_delivery`
  scanned the final request, and the scanner only recognised JSON strings.
- Prevention: anything that changes how data is written in the provider request must be checked against every
  reader of the final request bytes (evidence delivery, citations, size limits, conformance). New request formats
  need a deny-after-staging test, a receipt test and a citation test on both providers, with JSON as the control.
