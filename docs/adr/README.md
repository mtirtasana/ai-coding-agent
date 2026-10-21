# Architecture Decision Records

An Architecture Decision Record (ADR) explains a consequential choice: its
context, the decision, alternatives and consequences. Component documentation
explains how the resulting system works; an ADR explains why a particular
structure was selected. This distinction follows
[Michael Nygard's original ADR description](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions).

These records were written on **2026-10-20** after inspecting the implementation.
They retrospectively document the current design; the date is the recording
date, not an invented historical approval date. “Accepted” means the record
reflects the design currently used by this repository. It does not assert a
formal review process that did not occur.

## Decision index

| ID | Decision | Status | Main components |
| --- | --- | --- | --- |
| [0001](0001-ports-and-adapters.md) | Separate the application through ports and adapters | Accepted | Core ports, adapters, CLI |
| [0002](0002-bounded-sequential-loop.md) | Use a bounded sequential model/tool loop | Accepted | Agent engine |
| [0003](0003-provider-neutral-contracts.md) | Own the message and result contracts | Accepted | Types and providers |
| [0004](0004-capability-based-planning.md) | Enforce planning with executable capability policy | Accepted | Mode policy and tool registry |
| [0005](0005-session-state-and-project-instructions.md) | Retain completed turns and explicit instruction snapshots | Accepted | Session, terminal, instruction loader |
| [0006](0006-atomic-context-projection.md) | Project complete history units within a character budget | Accepted | Context builder |
| [0007](0007-workspace-guards-without-sandbox.md) | Apply workspace guards without claiming OS isolation | Accepted | Workspace and file tools |
| [0008](0008-hash-checked-targeted-edits.md) | Use hash-checked unique text replacement | Accepted | Editing adapter |
| [0009](0009-explicit-bounded-process-execution.md) | Execute explicit host commands with bounded capture | Accepted | Process runner and CLI |
| [0010](0010-separate-verification-evidence.md) | Keep verification outcomes separate | Accepted | Tests, lint, security, audit and CI |
| [0011](0011-metrics-and-independent-evaluation.md) | Report measured usage and independently checked fixtures | Accepted | Events and evaluation |
| [0012](0012-defer-persistence-and-caching.md) | Defer durable persistence and caching | Partially superseded by 0020 | Conversation/cache deferral remains |
| [0013](0013-vscode-workspace-extension.md) | Add a workspace-host VS Code interface over stdio | Accepted | Sidebar and bridge |
| [0014](0014-editor-buffer-ownership.md) | Review edits and let the editor own buffers | Accepted | Editor ToolExecutor |
| [0015](0015-cancellable-editor-workers.md) | Separate completions and coordinate cancellation | Accepted | Workers and runner |
| [0016](0016-managed-runtime-and-vsix.md) | Ship a managed backend and allowlisted VSIX | Accepted | Setup and packaging |
| [0017](0017-focused-file-reads.md) | Prefer bounded excerpts and measure task-level context cost | Accepted | Retrieval, prompts and evaluation |
| [0018](0018-interactive-cli-workflow.md) | Add bounded attachments and opt-in reviewed terminal writes | Accepted | CLI, prompts and presentation |
| [0019](0019-provider-profiles-and-keychains.md) | Named CLI profiles and OS-keychain references | Accepted | Connection controller and profile store |
| [0020](0020-host-local-usage-ledger.md) | Host-local metadata ledger with incomplete-usage coverage | Accepted | Metered provider, SQLite and report workers |
| [0021](0021-production-evidence-and-safety.md) | Supervised defaults and verification freshness | Accepted | File policy and typed evidence |
| [0022](0022-opt-in-recovery.md) | Opt-in completed-turn recovery | Accepted | Local snapshots and resume |
| [0023](0023-locked-internal-releases.md) | Locked runtimes and explicit release gates | Accepted | Setup, locks and artifact manifest |

Records 0020 and 0022 partially supersede record 0012 for usage and opt-in
conversation snapshots. Artifact caching remains deferred. Records 0019–0023 were recorded
on 2026-10-21.

## Record lifecycle

Use `Proposed` for an unadopted choice, `Accepted` for the current choice,
`Rejected` for a considered alternative that was not adopted, and `Superseded`
for a former choice replaced by another ADR. Record the replacement link in both
records. Do not reuse numbers. A record can contain an accepted decision to defer
work; describe the unimplemented work explicitly rather than conflating decision
status with implementation status.

When changing architecture, state the observable problem and compare realistic
alternatives. A new abstraction is justified by its use and tradeoffs, not by a
pattern name alone. Give source/test evidence for implemented claims and label
performance or quality expectations as hypotheses until measured.

Use [this template](template.md). The [component reference](../components.md)
provides the shared terminology and formal model.
