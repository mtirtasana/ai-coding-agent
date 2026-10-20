# ADR-0012: Defer durable persistence and caching

- Status: Accepted — decision to defer implementation
- Recorded: 2026-10-20
- Scope: future storage and context optimization
- Supersedes: none

## Context

Sessions currently live in one process. Durable history could enable restart,
artifact reuse and analysis, and a prior design proposal explores SQLite. Yet
storing history does not itself reduce what is sent to a model. Caches also need
correct invalidation after internal and external edits.

## Decision

Retain the implemented in-memory conversation state and deterministic context
projection. Defer SQLite, storage ports, migration logic, cache adapters,
automatic summaries and semantic retrieval until concrete requirements and
measurements justify them. Keep the existing SQLite/data-model document labeled
as a proposal, not an implemented contract.

This record accepts the deferral, not a completed database design. A future
storage decision should have its own ADR and refer back here.

## Alternatives considered

SQLite offers local transactional storage with low deployment overhead. Plain
JSON files simplify initial persistence but complicate concurrent updates and
queries. A remote database supports shared services at greater operational cost.
A model-response cache could avoid repeated requests but needs keys covering
context, tools, model/settings and relevant workspace state. None is required for
the current single-process session.

## Consequences

No migrations, retention jobs or cache invalidation are needed today, but sessions
are lost at exit and repeated reads are not cached. Full history still consumes
memory. Persisting history alone would leave transmitted context unchanged.
Database commits would not be atomic with filesystem edits, so recovery must
reconcile observed state rather than replay side effects blindly.

## Verification and evidence

See [ConversationState](../../agent/core/types.py),
[context selection](../../agent/core/context.py), and the explicitly future
[context/SQLite proposal](../context-and-persistence.md). No persistence adapter
or cache benchmark currently establishes operational benefit.

## Revisit when

Users need restart/resume, measured repeated-work cost justifies caching, or
multi-process access becomes necessary. Define retention/privacy, workspace
identity, content/version-based invalidation, migrations, concurrency and recovery
acceptance criteria before implementing storage.
