# Runtime 0.9.27

Runtime requires OS confinement for custom CLI tools and their version probes by
default. The YAML and Python APIs use the same default. Missing OS enforcement
fails preflight before inference, including for CLI manifests without a version
probe.

## Upgrade an existing deployment

1. Upgrade Runtime and Harness together if you use Harness to resume saved
   workflows or sessions. With unchanged YAML or direct Python configuration,
   CLI-free frozen turns retain their identities, including when the old
   configuration explicitly selected `required`.
2. For custom CLI tools, run preflight under the deployment's service account
   before rollout. Linux needs operational Bubblewrap 0.12.0 or newer at
   `/usr/bin/bwrap`. Use the [qualified Docker worker policy](https://github.com/B3Cognition/prosaic-integration-lab/blob/8d63e81/docs/sandboxed-tools.md#qualify-the-linux-image)
   to permit namespace setup. macOS needs operational `/usr/bin/sandbox-exec`.
3. To finish a saved CLI workflow under its original off-mode policy, explicitly
   set `cli_sandbox: {mode: 'off'}` in trusted Runtime YAML and keep the rest of its
   bindings. This uses the worker's host permissions. To switch that workflow to
   required mode, start a new run and obtain fresh approvals.

Python code that copies the nested `CliSandboxConfig` with
`dataclasses.replace` or serializes it as an explicit mode should reconstruct
saved records with the mode they originally used. Set `mode='off'` for
records written with the former default, or `mode='required'` if that was
explicitly set. For new work, construct `CliSandboxConfig(runtime_roots=...)`
to use the current default without turning it into an explicit policy.

Python callbacks and validators run as trusted in-process code. The release
requires no PostgreSQL recorder version or schema change.

See the [CLI tool guide](cli-tools.md#cli-sandbox) for supported backends and
operator configuration.
