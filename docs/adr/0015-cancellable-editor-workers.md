# ADR-0015: Separate completion workers and cancel effects before replacement

- Status: Accepted
- Recorded: 2026-10-20
- Scope: editor cancellation, checkpoints and inline inference
- Supersedes: none; extends ADR-0002 and ADR-0005

## Context

The synchronous engine can wait on a provider or UI approval. Killing its parent
without coordinating command cleanup could leave separately spawned processes.
Inline suggestions should not wait for a long chat run or alter its conversation.

## Decision

Use separate agent/completion workers. Add optional cooperative cancellation to
the core and runner. Synchronize command registration with cancellation, reject
pending callbacks, prohibit further tool work/turn commit, and await tracked
command cleanup before replacing a worker. Retain only completed checkpoints in
extension memory. Completions have no tools/history, bounded context/output,
one in-flight request, debounce and stale-result checks; automatic requests
require explicit workspace opt-in.

## Alternatives considered

One worker serializes suggestions behind chat. Hard parent termination alone
cannot guarantee cleanup of separate process groups. Durable recovery would need
storage/side-effect reconciliation beyond the current in-memory checkpoint.
Cancelling only the UI leaves late responses able to mutate state.

## Consequences

Provider calls already received may still run or incur costs. Accepted edits
remain after cancellation. Cleanup failure is reported and blocks ordinary retry;
arbitrary escaped descendants remain outside the guarantee. Window restart loses
checkpoints. No model-response cache or autonomous background review is introduced.

## Verification and evidence

See [cancellation token](../../agent/core/cancellation.py),
[runner](../../agent/adapters/execution.py),
[server tests](../../tests/test_server.py) and
[transport test](../../vscode/test/rpc.test.cjs).

## Revisit when

Providers expose reliable request cancellation, concurrency needs exceed two
lanes, or strict process isolation/end-to-end cost guarantees become requirements.
Obtain platform evidence before strengthening the stated cleanup guarantee.
