# Runtime 0.9.6 release

Authorized by the maintainer on 2026-10-17: publish the ready native-tool fixes,
then resume milestones. This branch combines published 0.9.5 with the previously
qualified finalization/envelope candidate. Historical candidate evidence remains
unchanged. Default execution behavior is unchanged; both controls are opt-in.

Release sequence: full source tests; installed-wheel checks; fresh review; push
and PR; exact-source Publish SDK qualification across required native platforms
and PostgreSQL cells; merge; qualify the exact merged commit; tag and promote
the same original bytes; verify public downloads. No live model rerun is needed.

Integration Lab adopts the verified public wheel only after publication. Its
experimental memory examples do not establish universal model improvement.

Local verification: the clean B3 environment completed 1,219 source tests; the
two archive tests initially lacked setuptools, then both passed against the exact
staged Git tree after installing the declared build tools. The one source skip
is the separately mandatory installed-wheel metadata gate, which passed. Installed
ownership, production and conversation smokes passed, with clean dependency checks.
An initial legacy-family environment run was abandoned and is not qualification.
Fresh review found no blocking Runtime integration issue. Required native CI must
pass before publication; local checks do not replace its platform/database gates.
