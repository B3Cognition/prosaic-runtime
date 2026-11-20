# Runtime 0.9.27

Runtime requires OS confinement for custom CLI tools and their version probes by
default. The YAML and Python APIs use the same default. Missing OS enforcement
fails preflight before inference, including for CLI manifests without a version
probe.

Linux deployments need an operational Bubblewrap 0.12.0 or newer at
`/usr/bin/bwrap`. Run preflight in the deployed image or host policy before
upgrading a service that grants custom CLI tools. A trusted host can explicitly
select `cli_sandbox: {mode: 'off'}` for a compatibility workload that requires
host execution privileges. Python callbacks and validators run as trusted
in-process code.

Use Harness 0.9.20 for the coordinated upgrade of saved workflows and
conversations. Native bundles retain their original sandbox policy in
their identities, so reconstruct earlier bundles using their original mode.

See the [CLI tool guide](cli-tools.md#cli-sandbox) for supported backends and
operator configuration.
