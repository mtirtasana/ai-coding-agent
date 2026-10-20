# ADR-0016: Ship a managed backend wheel in an allowlisted VSIX

- Status: Accepted
- Recorded: 2026-10-20
- Scope: installation and local distribution
- Supersedes: none

## Context

The selected editor workflow needs a Python backend on the workspace host, with
explicit setup rather than a hidden install on project open. A VSIX should contain
only runtime assets. The initially evaluated packaging CLI introduced an
unpatched transitive development-tool advisory; no advisory suppression was used.

## Decision

Bundle a backend wheel and its checksum/version manifest. Setup uses a chosen
Python 3.11+ interpreter to create an extension-managed environment and install
selected provider dependencies after an explicit action. Validate readiness
against the packaged wheel; invalidate the readiness marker during updates.
Build the private VSIX with standard-library ZIP/XML and an explicit asset list,
then verify installation in native VS Code tests. Keep npm dependencies locked
and audit the development tree. Do not publish automatically.

## Alternatives considered

Requiring users to maintain an agent installation reduces setup code but was not
the selected experience. Bundling Python binaries expands platform distributions.
A packaging CLI provides extra Marketplace validation but adds dependencies that
are unnecessary for this private artifact. A hosted service changes the deployment
and credential boundary.

## Consequences

Python is still a prerequisite, package downloads need network access, and failed
setup requires correction/retry. The wheel checksum checks consistency, not
publisher authenticity. The small packager needs native installation coverage and
must be revisited for Marketplace distribution. No license grant is inferred by
packaging the existing project as an unpublished extension.

## Verification and evidence

See [runtime](../../vscode/src/runtime.ts),
[packager](../../vscode/scripts/package.py),
[integration launcher](../../vscode/scripts/integration.cjs) and
[extension guide](../../vscode/README.md).

## Revisit when

Marketplace publication, signed distribution, offline deployment or bundled
interpreters become requirements, or a maintained packaging tool removes the
tradeoff that motivated this small allowlisted builder.
