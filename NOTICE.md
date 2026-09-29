# Provenance

The OpenAI-compatible transport, agent loop, compaction, path filters, progress helpers and transcript writer were extracted from B3Cognition/echelon at commit `3c606aa1ef9e44cc587ba122648392e0250f3094`.

The initial repository history preserves commits touching the five extracted implementation files and LICENSE, with `src/harness/ai_cli_backends/` renamed to `src/prosaic_runtime/`. Original author metadata and commit messages are retained. Commit IDs change because the history is filtered. The source repository and full context are available at https://github.com/B3Cognition/echelon.

Transport conformance tests were adapted from Echelon's `tests/unit/test_ai_cli_backend.py` at the same commit. Echelon-specific tools and control protocols remain in Echelon.

Prosaic Runtime is distributed under the Apache License, Version 2.0; see LICENSE.
Runtime integration and API work is Copyright (c) 2026 B3 Cognition.

The extracted code was originally distributed under MIT. Its original notice,
`Copyright (c) 2026 Testimonial`, and permission text are retained in LICENSE-MIT.
This historical attribution does not replace the project's Apache-2.0 license.
Third-party components retain their own licenses. Previously published versions
and tags retain the license notices shipped with them.
