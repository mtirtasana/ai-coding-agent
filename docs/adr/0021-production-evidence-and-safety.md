# ADR-0021: Supervised defaults and host-owned verification evidence

- Status: Accepted
- Recorded: 2026-10-21
- Scope: CLI/editor safety and check freshness
- Partially supersedes: [ADR-0018](0018-interactive-cli-workflow.md), for the default CLI review policy

## Context

Independent check tools exist, but successful process exits and model statements
do not establish that checks apply after further edits. Direct CLI writes were
the default, while the chosen audience needs supervised use of trusted projects.

## Decision

Default writable CLI runs to review, requiring an explicit opt-out for automation.
Use atomic replacements in both modes. Add shared discovery exclusions and
non-overridable credential/agent-denial policy. Record typed check outcomes and
bounded content/configuration fingerprints around actual execution. Later edits,
editor events and configuration changes invalidate passing evidence; uncertainty
is unknown. Model text cannot create evidence. Restart/resume resets freshness.
Expose evidence in run results, events and human summaries, including failures.

## Alternatives and consequences

Prompt instructions alone cannot enforce freshness. A persisted verification
ledger would introduce recovery claims and stale external dependencies without
solving them. The in-memory tracker is conservative and may mark a valid check
stale after harmless filesystem events. Fingerprinting adds bounded I/O and
excludes generated/private paths; it does not capture all environmental causes.
Trusted host commands remain outside file review and OS isolation.

## Evidence and revisit conditions

[Verification adapter](../../agent/adapters/verification.py),
[production regressions](../../tests/test_production.py), and the
[baseline guide](../production-readiness.md). Revisit for measured fingerprint
cost, richer build-system input tracking, or a requirement for isolated execution.
