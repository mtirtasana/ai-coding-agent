# ADR-0007: Apply workspace guards without claiming OS isolation

- Status: Accepted
- Recorded: 2026-10-20
- Scope: filesystem access and trust boundary
- Supersedes: none

## Context

Model-selected paths can escape the intended project or expose common credential
locations. A local development tool needs predictable access rules while remaining
small enough to run without container infrastructure. Application path validation
cannot restrict arbitrary host processes.

## Decision

Resolve each file-tool path under a resolved workspace root. Require containment
and reject protected components in both original and resolved paths: `.git`,
`.ssh`, `.aws` and names beginning with `.env`. Retain read-size/write-permission
checks. Validate full-file content as UTF-8 before creating directories or opening
a target, avoiding truncation on an encoding failure.

Treat the selected workspace, endpoint and configured commands as trusted inputs.
Document that no OS sandbox is implemented. Recommend an isolated execution
environment when the repository or commands are untrusted.

## Alternatives considered

Lexical `..` filtering alone misses symlink escapes. OS isolation could restrict
processes and race-sensitive path access more strongly, but requires explicit
filesystem, environment and network policies. Removing execution entirely would
prevent actual local verification.

## Consequences

Common traversal, protected-path access and stable outside-root symlinks are
blocked through file tools. Concurrent symlink manipulation remains possible.
The protection is not an exhaustive secrets detector. Model context can contain
other sensitive project content. Whole-file writes still replace directly and
are not crash-atomic. Configured subprocesses inherit host permissions and are
not constrained by these path checks.

## Verification and evidence

See [files.py](../../agent/files.py), [test_files.py](../../tests/test_files.py),
and [security limits](../security-checks.md). Tests exercise ordinary traversal,
symlink escape, read-only behavior and malformed-Unicode write preservation, not
all concurrent adversarial interleavings.

## Revisit when

Running untrusted repositories becomes a supported use case, concurrent mutation
is expected, or secret-handling requirements exceed these application guards.
Specify and test an OS boundary rather than extending a filename denylist into
a supposed sandbox.
