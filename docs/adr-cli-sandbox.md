# ADR: Separate authenticated inference from sandboxed CLI tools

**Status:** Accepted; included in Runtime 0.5.2 / Harness 0.4.2
**Date:** 2026-10-01
**Deciders:** Project operator before production rollout

## Context

Fixed argv and path-argument checks do not prevent an executable from independently
reading host credentials or connecting to the network. Native coding providers
also need authentication/model network access, so one deny-all policy around the
whole provider would break execution without separating these authorities.

## Decision

Keep authenticated inference in the controller. Add operator-selected
`cli_sandbox.mode: required` around Runtime CLI children, including version
probes. Backends are macOS Seatbelt and Linux Bubblewrap; unsupported platforms fail closed
without an unrestricted fallback. Existing configs default to `off` for
compatibility. Prose/manifests cannot change this policy.

Children receive a private temporary HOME/scratch directory, read-only evidence
and trusted executable/interpreter/library paths. Extra dependencies require
explicit `runtime_roots`. There is no host IP network or non-scratch write
grant; forbidden paths override read grants. Linux uses a read-only synthetic
root, read-only mounts, private scratch, dropped capabilities, disabled nested
user namespaces and isolated PID/IPC/network/UTS namespaces. Seatbelt denies IPC;
on Linux, do not mount directories containing host Unix sockets. Endpoint keys are not implicitly
inherited. Explicit manifest `pass_env` remains an intentional host grant.

Harness binds non-default policy to workflow identity and checks injected
adapter capability/policy. Default-off preserves old checkpoint fingerprints.
This does not isolate Python callbacks, validators, or native Codex/Claude.

## Options considered

| Option | Benefit | Limitation |
| --- | --- | --- |
| Prompt rules and argv/path checks | Simple, compatible | Cannot contain executable side effects |
| macOS Seatbelt per CLI | Available here; real OS checks; inference auth unaffected | Platform-specific, dependencies need bounded grants |
| Linux Bubblewrap per CLI | Filesystem and namespace isolation, no architecture-specific loader paths | Requires patched Bubblewrap and kernel/host permission for user namespaces |
| Strict sandbox around native provider | Broad containment in principle | Mixed provider/tool auth and networking require a broker or verified native separation |

## Trade-offs and consequences

This is an incremental CLI boundary, not a claim that all execution is isolated.
Off mode remains a migration risk. Granting HOME or `/` as a runtime/evidence
root defeats read isolation. Dependencies, configuration, checks and executables
remain trusted; granted data can itself contain secrets. Metadata is visible.
This is not a VM, dependency-integrity system, resource-DoS defense, or proof
against OS vulnerabilities and all covert channels.

Bubblewrap also needs a correct policy, not just installation; see its
[upstream security guidance](https://github.com/containers/bubblewrap/blob/main/README.md).
Require Bubblewrap >= 0.12.0 for its
[setup symlink-escape fix](https://github.com/containers/bubblewrap/security/advisories/GHSA-pxhw-h44j-8pfx).
Preflight checks a real trusted interpreter launch inside the sandbox. CI includes
native `ubuntu-24.04-arm` and AMD64 runners, building the fixed upstream commit
`2a76602a8c71f36c1527cf9fc3417d9149822e0c` (v0.12.0) instead of assuming an
older distribution package is safe. A mandatory backend gate prevents skipped
security tests from being interpreted as validation.

## Action items

1. [x] Implement fail-closed CLI isolation and safe diagnostics.
2. [x] Test real file/alias/write/network denial, child policy, probe isolation,
   scratch cleanup and an unrestricted positive control.
3. [x] Bind Harness adapter policy and durable approval identity.
4. [x] Run the synthetic probe against the local model endpoint.
5. [ ] Review/version/release and pin downstream.
6. [x] Implement and OS-test a Linux ARM64 backend; AMD64 CI validation remains pending.
7. [ ] Design native-provider auth/model-network separation before restricting
   Echelon's whole process.
