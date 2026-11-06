# Guarded ecosystem release plan

The maintainer authorized merging and releasing Runtime, Harness and Lab, after
the separate Lab 0.9.5 release completed. That release completed with public
downloads and fresh consumer verification before this work started.

- [ ] Rerun reviewed Runtime #16 source and independent regressions without
  changing saved expectations; merge its expected head after verification.
- [ ] Prepare Runtime 0.9.14 version/metadata/workflow pins and this release
  guide. Verify unchanged production implementation, focused version contracts,
  build and isolated wheel probes. Merge the release PR with green exact-head CI.
- [ ] Build/qualify immutable Runtime artifacts on the merged source with Core
  0.5.0 lineage, tag and promote the same bytes, verify public index and imports.
- [ ] Update Harness #12 to the feature Runtime release, prepare the next free
  Harness version, rerun full/installed/saved compatibility and platform/PG
  qualification; merge, publish and verify public bytes with exact lineage.
- [ ] Update Lab #46 and its #45 precursor onto Lab 0.9.5, pin the actual released
  feature SDK wheels and checksums, and qualify source, both synthetic wires,
  file/PG recovery, all four real-stack cells and distribution/reasoning lanes.
- [ ] Merge the tested Lab release tree, publish original CI artifacts, verify
  anonymous downloads and fresh wheel/rebuilt-source adoption, then stop the
  continuation automation and report versions, links and remaining limits.

Do not modify ordinary dirty user checkouts, old releases, saved regressions or
live provider evidence. No live inference. Do not dismiss prior reviews or
resolve GitHub threads automatically. New test failures require diagnosis and
correction, never weaker assertions. The existing native execution method
continues; the release instruction supplies integration authorization.
