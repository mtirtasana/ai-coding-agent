# ADR-0023: Hash-pinned runtime installation and explicit release-candidate gates

- Status: Accepted
- Recorded: 2026-10-21
- Scope: Runtime setup, compatibility and internal artifacts
- Extends: [ADR-0016](0016-managed-runtime-and-vsix.md)

## Context

An in-place runtime update can leave a broken environment after a network or pip
failure. Broad dependency ranges allow later installs to resolve different code.
Passing demo tests does not validate live model behavior or unlocked OS vaults.

## Decision

Generate/review pinned runtime dependency versions and wheel hashes explicitly.
Verify the bundled wheel and lock manifests. Install into a separate candidate
environment without unconstrained dependency resolution, validate protocol 2,
and publish its pointer atomically only after readiness. Preserve the previous
runtime; prevent overlapping setup with an exclusive lock. Test locked installs
on supported OSes at the oldest/newest supported Python versions.
Assemble wheel/VSIX, inventory, checksums, guide, changelog and an explicit manual
checklist as internal release-candidate artifacts. Do not publish to registries
or claim production readiness while live-provider/vault gates remain pending.

## Alternatives and consequences

In-place pip upgrades offer simpler paths but cannot preserve a working runtime.
Bundling every platform's full environment would enlarge distribution considerably.
Hash-pinned network installation is repeatable for reviewed wheels but still
depends on index availability and proxy/CA configuration. Old runtimes consume
disk until manually removed. This is integrity verification, not a signing or
confidential distribution service. Public repository/Actions access still applies.

## Evidence and revisit conditions

[Runtime installer](../../vscode/src/runtime.ts), [locks](../../vscode/backend/runtime-openai.txt),
[release assembly](../../tools/release_manifest.py), and the
[release procedure](../production-readiness.md). Revisit for offline installation,
signed public publication, automatic runtime cleanup or a larger support matrix.
