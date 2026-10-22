# ADR-0020: Record model-call metadata in a host-local SQLite ledger

- Status: Accepted
- Recorded: 2026-10-21
- Scope: CLI and editor agent/completion usage statistics
- Partially supersedes: [ADR-0012](0012-defer-persistence-and-caching.md), for usage metadata only

## Context

Per-run console metrics cannot answer how many tokens were reported during a
conversation, calendar period, or across CLI/editor processes. Multiple editor
workers and standalone CLI calls need a shared host-local queryable history.
This requirement does not justify persisting conversations or caching responses.

## Decision

Wrap the model port in `MeteredProvider` and record attempts before generation,
then returned usage before cancellation or completion filtering. Use a request
UUID primary key, conversation/run identifiers, UTC timestamps, source/category
labels and nullable provider token counts. Retain coverage and unknowns. Use
SQLite WAL, parameterized SQL, bounded waits and versioned schema, outside the
workspace. Read-only reports can run before initialization and while coding.
Calendar boundaries follow the execution host's local timezone; rolling periods
use elapsed hours. Exclude prompts, source, credentials, endpoints and raw errors.

On storage failure keep process-local metadata, warn, and continue coding. Merge
dirty rows by request ID with persistent aggregates. The editor collects live
worker snapshots without persisting them through a report request. Mark reports
incomplete when storage or a worker snapshot is unavailable. No billing estimate,
cross-host synchronization or automatic retention is added.

## Alternatives and consequences

JSONL would require replay/deduplication and safe concurrent appends for queries.
A remote service adds deployment and privacy overhead. Existing run observers
remain useful for immediate feedback but cannot supply historical statistics.
SQLite is in Python's standard library and supports independent local processes.
Writes incur bounded local I/O and need an accessible state directory. Crashed
attempts remain unfinished; failed responses have unknown tokens. SDK retries or
usage never returned by providers cannot be accounted for exactly. Memory-only
records disappear at worker exit. Users must manage retention and backups.

## Verification and revisit conditions

See [ledger](../../agent/adapters/usage.py), [tests](../../tests/test_connections_usage.py),
and the detailed [contracts and diagrams](../providers-and-usage.md).
Revisit for measured ledger size/performance problems, configurable retention,
billing reconciliation, schema migration or explicit cross-host aggregation.
Conversation persistence, cache invalidation and artifact reuse remain deferred
under ADR-0012. This decision changes observability, not model context selection.
