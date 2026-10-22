# ADR-0014: Let VS Code own individually reviewed buffer edits

- Status: Accepted
- Recorded: 2026-10-20
- Scope: editor file access and verification prerequisites
- Supersedes: none; adds an editor adapter beside ADR-0008

## Context

Disk-only reads miss unsaved work, while backend filesystem writes bypass editor
Undo and can overwrite a newer buffer. A broad “allow writes” switch is less
specific than reviewing each proposed edit. Disk checks cannot see unsaved buffers.

## Decision

Route editor file tools through validated host callbacks. Read current editor
text and hash it. Review each creation/modification in a native diff, then check
canonical target, version and content again before editor application. Keep
existing buffers unsaved. Require session permission and explicit saving before
configured checks. Exclude unrestricted lint-fix commands from this adapter.
Keep the CLI filesystem adapter and its existing opt-in fixes unchanged.

## Alternatives considered

Direct backend writes are simpler but conflict with editor state and Undo.
Authorizing an entire implementation run reduces prompts but is not the selected
interaction model. Automatically saving would change user state without the
explicit save decision chosen for this release.

## Consequences

Rejected/stale proposals fail without applying the proposed edit. Accepted edits
can remain after a run fails or is cancelled; Undo is a user operation, not an
agent rollback. Buffer hashes differ conceptually from CLI byte snapshots when
encoding conventions differ. File tools remain restricted to eligible UTF-8 text.
Application guards still do not provide OS isolation or multi-writer transactions.

## Verification and evidence

See [editor tools](../../vscode/src/editor.ts),
[editor tests](../../vscode/test/editor.test.cjs) and
[native tests](../../vscode/test/native.cjs).

## Revisit when

Users request run-level edit authorization, multi-file atomic changes or explicit
save policies. Extend the interaction and concurrency contract rather than
silently switching editor tools back to filesystem writes.
