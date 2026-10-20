# ADR-0018: Add bounded CLI attachments and opt-in reviewed writes

- Status: Accepted
- Recorded: 2026-10-20
- Scope: terminal interaction and CLI presentation
- Supersedes: none

## Context

The CLI already has modes, command completion and configured checks, but verbose
JSON is its main progress interface. Tasks are awkward to compose across lines,
file context requires ordinary model retrieval, and write permission permits
direct effects. The selected workflow keeps direct edits available and adds
individual review only when requested.

## Decision

Keep prompt-toolkit optional. Enter submits and Alt+Enter adds a line. Add local
status and attachment commands; send bounded, fresh snapshots with explicit user
tasks and retain pending selections after failures. Keep sessions memory-only.

Use a CLI presentation observer and a validated tool adapter for progress,
check results and change counts. Add a core tool-start event without coupling
the core to terminal code. Human output and verbose JSON remain on separate
streams; terminal controls in untrusted text are escaped and provider errors
use controlled messages. Report token coverage separately from character counts.

Enable per-edit Apply/Reject with --review-edits, off by default. Require explicit
write permission and a terminal. Recheck the prepared target/content after the
decision, atomically replace existing files, and create new files without
overwriting race winners. The dedicated lint fixer is incompatible with this
flag; configured checks remain host processes outside file-tool review.

## Alternatives considered

Default-on review was not selected. A full-screen TUI or additional rendering
dependency would expand the first iteration. Persisted prompts and configuration
profiles address separate lifecycle problems. Routing changes or AI summaries
would affect token/quality behavior rather than terminal composition.

## Consequences

No new mandatory dependencies or implicit model calls are introduced. Attachments
increase context deliberately, and completed attachment contents remain in normal
conversation history. Long diffs can require terminal scrollback. Reviewed new
files require filesystem hard-link support and retain private staging permissions.
Optimistic file checks do not provide locking, rollback or command isolation.

## Verification and evidence

See the [terminal guide](../interactive.md),
[workflow tests](../../tests/test_cli_workflow.py),
[presentation adapter](../../agent/adapters/console.py) and
[review adapter](../../agent/adapters/review.py). A real PTY smoke also checks a
scripted model workflow with accepted edits, actual verification and JSON logs.

## Revisit when

Persistent sessions, scripting APIs, full-screen navigation, live response
streaming or non-hard-link filesystems become supported product requirements.
