# Lessons

## TOON tool results skipped evidence delivery checks (PR #42 review)

- What: with `tool_result_format: toon`, sent tool results were missing from evidence delivery tickets. Use was
  not authorized again before dispatch, delivery receipts were empty and valid required citations failed.
- Root cause: the request encoder turned tool results into TOON before `EvidenceRegistryV1.prepare_delivery`
  scanned the final request, and the scanner only recognised JSON strings.
- Prevention: anything that changes how data is written in the provider request must be checked against every
  reader of the final request bytes (evidence delivery, citations, size limits, conformance). New request formats
  need a deny-after-staging test, a receipt test and a citation test on both providers, with JSON as the control.

## Evidence scanner rejected TOON-looking user text (PR #42 follow-up review)

- What: with the TOON library installed and evidence bindings on, a user message that decoded as deeply nested
  TOON made the turn fail with `observationFailure` before any provider request, even on the default JSON profile.
- Root cause: `prepare_delivery` tried to decode every request string as TOON, not just tool results, and the
  decoded value went to `digest`, whose bounded encoder raises `ValueError` past 64 levels of nesting.
- Prevention: only interpret text in the places a format is written (tool messages and `tool_result` blocks).
  Treat a value past the digest bounds as a non-match, since every staged projection fits them. Readers of the
  final request must never fail the request over content they don't own.
