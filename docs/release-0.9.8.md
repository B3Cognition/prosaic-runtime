# Runtime 0.9.8

Adds opt-in native Claude Code/Codex CLI execution and unified pools combining
native profiles with direct API/local endpoints. The Python API and CLI require
independent native permissions and API grants, with explicit total and backend
deadlines, attempt caps, request/tool limits and quota identities.

Selection and cooldowns apply before work starts. Automatic switching requires
positive pre-work rejection proof. Partial text, reasoning, streamed activity,
tool work, authentication failures, timeouts and uncertain effects stop rather
than replaying work. Shared aliases do not create additional subscription quota.

Includes the reviewed replay-safety corrections and Darwin process-group cleanup
repair. Existing API execution, serialized contracts and Harness integrations
remain compatible. Vendor CLIs are installed and authenticated independently;
the release does not confer permission or create credentials.

Qualification binds one exact source commit and immutable wheel/sdist bytes to
source, installed-wheel, native confinement, PostgreSQL and dependency-floor
gates. Promotion reuses those bytes. Recorder 0.3.0 artifacts are compatibility
witnesses and are not republished. Lab adopts verified public release downloads
after publication. Historical live benchmarks remain tied to their original
candidate bytes; no new live-model ranking is claimed by this release.
