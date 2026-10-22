# ADR-0005: Retain completed turns and explicit instruction snapshots

- Status: Accepted
- Recorded: 2026-10-20
- Scope: conversation lifecycle and project rules
- Supersedes: none

## Context

Follow-up tasks need previous successful work and accepted plans. Partial model
turns can contain unmatched observations after an interruption. Project rules
should stay stable during a conversation, and a broken reload should not erase a
usable session.

## Decision

Keep mutable `ConversationState` in memory. Commit messages only after a final
assistant response. Store the latest successful plan separately from the accepted
plan. Load only workspace-root `AGENTS.md` as a validated snapshot, pin it in
context, and refresh it explicitly on `/new`. Read the replacement successfully
before clearing state. Do not discover nested instruction files automatically.

## Alternatives considered

Committing every message supports finer-grained recovery but needs partial-turn
and side-effect reconciliation. Reloading instructions before each call can
change policy unexpectedly. Hierarchical instruction discovery needs explicit
scope and precedence rules. Durable session storage adds migration, retention
and recovery requirements addressed by [ADR-0012](0012-defer-persistence-and-caching.md).

## Consequences

A failed turn preserves the previous completed conversation and successful plan.
Its file/process side effects may remain. The instruction snapshot can become
outdated until reload. Restarting loses history and plans. Project text is prompt
guidance and does not override executable mode checks; pinning it does not solve
prompt injection. Successful conversation commit is not a filesystem transaction.

## Verification and evidence

See [engine.py](../../agent/core/engine.py),
[instructions.py](../../agent/adapters/instructions.py),
[terminal.py](../../agent/terminal.py),
[test_session.py](../../tests/test_session.py) and
[test_terminal.py](../../tests/test_terminal.py).

## Revisit when

Crash recovery or multi-process sessions become real requirements, users need
scoped nested rules, or long sessions need controlled instruction refresh without
resetting conversation. Specify versioning and reconciliation before changing
commit semantics.
