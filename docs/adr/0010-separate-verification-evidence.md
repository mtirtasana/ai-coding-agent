# ADR-0010: Keep verification outcomes separate

- Status: Accepted
- Recorded: 2026-10-20
- Scope: tests, lint, security and CI
- Supersedes: none

## Context

Behavioral tests, lint, static security analysis and dependency advisory lookups
answer different questions. Reporting one generic “checks passed” state obscures
missing tools, network failures and untested behavior. A fixer also changes the
state to which previous tests applied.

## Decision

Provide independent `run_tests`, `run_lint`, `run_security` and
`run_dependency_audit` tools when configured. Preserve each process result and
request separate reporting. Supply optional `run_lint_fix` only with write
permission and a configured lint recheck. Recheck lint after a normal fixer
result; skip it on fixer timeout; return `requires_test_rerun=True`.

Use Ruff, Bandit and pip-audit in CI, with separate functional/lint, security and
dependency jobs. Maintain only individually justified Bandit exceptions. Do not
automatically fix security findings, upgrade dependencies or ignore advisory IDs.

## Alternatives considered

One combined command is simple to invoke but obscures coverage and failure
classification. Automatic fixes reduce interaction but can alter semantics and
require review. A verification ledger could enforce result freshness, but has
not been implemented.

## Consequences

Failures are attributable to a check category. A scanner's nonzero exit still
needs output interpretation; infrastructure failure is not evidence of a code
vulnerability. Passing scans do not establish OWASP compliance. Test reruns and
freshness are requested by prompt/metadata, not universally enforced. CI audits
installed dependencies, including optional tooling, rather than every version
permitted by dependency ranges.

## Verification and evidence

See [workflow](../../.github/workflows/tests.yml),
[test_linting.py](../../tests/test_linting.py),
[test_security.py](../../tests/test_security.py), and
[scanner coverage and exceptions](../security-checks.md).

## Revisit when

Users need enforced completion criteria, persistent evidence tied to file hashes,
or language-specific verification. Add an explicit ledger or scanner-result
schema when that need outweighs its state and invalidation costs.
