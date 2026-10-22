# ADR-0022: Opt-in completed-turn recovery outside the usage ledger

- Status: Accepted
- Recorded: 2026-10-21
- Scope: Local CLI/editor conversation recovery
- Partially supersedes: [ADR-0012](0012-defer-persistence-and-caching.md), for completed conversation snapshots

## Context

The internal team needs restart recovery, while prompts can contain source and
secrets. The usage ledger deliberately excludes conversation content. Replaying
partial tool histories could repeat already accepted filesystem side effects.

## Decision

Keep recovery off by default and require a CLI flag or machine-scoped user setting.
Store versioned, size-bounded completed-turn JSON snapshots in private host-local
storage separately from SQLite usage. Replace snapshots atomically; failed saves
warn and preserve the prior version. Save provider references without credentials.
Validate workspace identity and completed history before resume, reload rules,
clear accepted plans and verification, and enter Plan without replaying tools.
Expire snapshots after 30 inactive days during session management. Preserve
corrupt/unknown schemas for inspection. Retain usage unless explicitly pruned.

## Alternatives and consequences

Default-on logging would retain source without a deliberate choice. Putting
messages into usage SQLite would weaken its metadata-only contract. Snapshot
replacement is simpler than event-sourced tool replay; it provides neither undo
nor multi-process conversation synchronization. Concurrent saves of one ID are
last-writer-wins. Stored message text can itself contain secrets; permissions
are not encryption. Disabling recovery does not delete prior snapshots.

## Evidence and revisit conditions

[Recovery adapter](../../agent/adapters/recovery.py), [tests](../../tests/test_production.py),
and [retention/resume contract](../production-readiness.md). Revisit if users need
encrypted snapshots, concurrent editing, storage migrations or explicit sharing.
Artifact caches, summaries and semantic retrieval remain deferred.
