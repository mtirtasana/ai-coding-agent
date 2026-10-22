# ADR-0025: Bounded batch observations, authenticated artifacts and explicit task retirement

- Status: Accepted
- Recorded: 2026-10-22
- Scope: observation tools, local artifact lifecycle and completed-task context
- Extends: [ADR-0024](0024-token-efficient-context.md)

## Context

Serial file reads introduce model round trips. Replaying observations consumes
input repeatedly. Existing file references require a fresh read, and temporary
check-log IDs cannot recover exact captured bytes across process exit. Unlimited
history retention and automatic summaries each have accuracy and privacy costs.

## Decision

Batch up to ten guarded ranges under a shared output budget. Preserve per-file
errors, full-file hashes and dirty-buffer versions. Revalidate at the end and
explicitly decline an atomic cross-file snapshot guarantee.

Use a workspace-bound artifact adapter with authenticated handles. The default
is bounded memory; opt-in persistent storage encrypts payloads and private
metadata with AES-256-GCM and authenticates full receipts with a derived HMAC
key. Use OS-vault keys or explicitly injected headless keys, never plaintext
key files. Enforce expiry, quotas, current source-path denial and policy binding.
Record exact captured bytes and capture limits rather than asserting unlimited
stdout retention. Execution status and correctness evidence remain distinct.

Combine opt-in intra-task aging with explicit path pins and user-initiated
completed-task sealing. Archive the exact validated checkpoint before replacing
history with a small historical handoff. Preserve the newest observations and
provider continuation requirements. Restore into Plan without replaying tools,
restoring approvals or claiming fresh verification.

Expose batch inspection and exact-byte retrieval through an optional read-only
stdio MCP adapter. Keep vendor installation manifests and global rules out of
the agent core. No inference call creates a handoff; the user supplies decisions
and unresolved work explicitly.

The optional MCP extra requires a security-patched AnyIO version. The real stdio
test retains initialization timeouts and needs local IPC/event-loop wakeups in
restricted environments. It makes no external model request and does not change
regular runtime locks.

## Alternatives and consequences

LLM summaries can omit dependencies or invent conclusions and add model cost.
Unbounded raw retention increases disk and confidentiality costs. Bare content
hashes do not authenticate receipts. Provider-specific plugin rules would couple
the core to one host. Global response caching needs a separate freshness policy.

Encrypted storage adds key management and optional dependencies. Old handles
may become unavailable through expiry, memory eviction, policy changes or key
loss. Quota/storage failure must preserve original history. Restoring a task
does not restore its filesystem snapshot. Context reduction may reduce prompt
cache reuse and require additional recovery requests; monetary savings are an
empirical question. Short tasks may gain no efficiency from sealing.

## Verification

Tests cover mixed/denied ranges, global output limits, dirty buffers, changing
files, cancellation, byte-exact recovery, encryption/restarts, metadata/ciphertext
tampering, wrong keys/workspaces, policy revocation, quotas/locks/expiry, vault
key handling, mandatory frontier observations, atomic failure of task retirement,
and restore without replay. Real MCP stdio tests verify the read-only tool list.
Native editor tests exercise batch reads and seal/read/restore through the real
backend. Live token/cost/accuracy improvements remain measurement gates.

Contracts, limits and user commands: [context artifacts](../context-artifacts.md).
