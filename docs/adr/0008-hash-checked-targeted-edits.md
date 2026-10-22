# ADR-0008: Use hash-checked unique text replacement

- Status: Accepted
- Recorded: 2026-10-20
- Scope: source editing
- Supersedes: none

## Context

Replacing a complete file requires retransmitting unrelated text and risks
unintended changes. Line-number patches can become stale after another edit.
The agent needs a small cross-language editing operation with a clear rejection
condition and inspectable diff.

## Decision

Add snapshots of exact UTF-8 bytes with SHA-256 version tokens. Require the
expected hash and exactly one occurrence of a nonempty old-text fragment,
including overlap checks. Preview a unified diff; on apply, recompute conditions,
write/fsync a same-directory temporary file, preserve permission bits, revalidate
original bytes/path and replace the target. Keep `write_file` for new files and
compatibility. Diff previews are available but not a required approval protocol.

## Alternatives considered

Whole-file replacement remains simple but has a larger accidental-change surface.
Line patches need hunk/location handling. AST edits can express semantics better
but require language-specific parsers and formatting policies. General patch
application would expand syntax and ambiguity handling beyond current needs.

## Consequences

The operation rejects stale snapshots and ambiguous fragments and preserves
unchanged byte sequences, including CRLF. Hashes provide version checks, not
authenticity. Exact matching can be inconvenient for repetitive code. Rechecking
is not an atomic compare-and-swap or lock: concurrent writes can occur before
replacement. Multiple edits are not a transaction, and directory crash durability
or full metadata/ACL preservation is not guaranteed.

## Verification and evidence

See [editing.py](../../agent/adapters/editing.py) and
[test_editing.py](../../tests/test_editing.py). These tests cover chosen stale,
ambiguous, binary, byte-preservation and failure cases, not proof of semantic
correctness or multi-writer serializability.

## Revisit when

Representative tasks show excessive ambiguous-match retries, language-aware
changes are needed, or concurrent editors require stronger coordination. Measure
edit accuracy and token use before replacing this small operation with a richer
patch engine.
