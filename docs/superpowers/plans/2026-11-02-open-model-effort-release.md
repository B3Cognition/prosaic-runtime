# Open-model reasoning examples and Runtime 0.9.10 release

User authorized examples, testing and release on 2026-11-02. Integrate the
reasoning feature onto origin/main 1869f583 (Runtime 0.9.9), preserving other
working-tree changes in the original checkout.

1. Verify current primary provider documentation. Add exact model capabilities
   and single-turn, tool-free DeepSeek V4 Pro, Qwen 3.8 Max and Nemotron NIM
   examples. Keep ultra Codex-only and optional custom capability overrides.
2. Test actual canonical Markdown inspection and CLI-to-HTTP payloads using a
   local fixture. Cover supported levels, pre-dispatch rejection and API none
   compatibility. Run the complete source suite and installed-wheel checks.
3. Prepare version 0.9.10, update version-bound release checks, obtain review
   and commit. Run the established exact-source qualification workflow.
4. Publish qualified immutable bytes through the existing tag promotion
   workflow, verify PyPI digests and create the GitHub release.

No provider keys are present in the execution environment. Fixture integration
tests validate control wiring; live hosted inference is not claimed.

## Pre-commit verification

- Final effort/discovery/completion/example selection: 160 passed in 65.38s.
- All 24 provider example and compatibility checks passed in 13.36s.
- Independent reviewer found no remaining blockers after constrained admission,
  custom API default compatibility and fingerprint binding fixes.
- The source archive metadata tests deliberately read committed HEAD. The
  pre-commit full-suite run used the previous release archive; restart after the
  version/source commit for release evidence.
- Version 0.9.10 retains the published recorder 0.3.0 dependency range.
