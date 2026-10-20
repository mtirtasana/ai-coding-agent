# ADR-0013: Add a VS Code workspace extension over stdio

- Status: Accepted
- Recorded: 2026-10-20
- Scope: editor interface and Python bridge
- Supersedes: none

## Context

The existing agent has provider-neutral ports but only terminal interaction.
Users need a dedicated sidebar, editor context, diagnostic actions and inline
completions on local and remote workspaces without requiring another AI product.

## Decision

Use a TypeScript workspace extension and a Python JSON-RPC stdio bridge. Keep
planning/context/provider behavior in Python; keep UI, documents, secrets and
review in the extension host. Run beside the selected workspace, including remote
hosts. Use a versioned handshake, bounded frames, controlled errors and explicit
bidirectional callbacks. Preserve the CLI. Deliver completed-response/progress
updates initially, not token streaming.

## Alternatives considered

A terminal launcher would reuse the CLI directly but cannot provide buffer-aware
review/completions. VS Code Chat integration would adopt its interface constraints.
An HTTP service adds listener/authentication/deployment requirements not needed
for parent-child communication. A TypeScript rewrite duplicates existing behavior.

## Consequences

The architecture reuses core ports while adding transport and lifecycle contracts.
Each selected folder has separate state; browser-only/virtual workspaces are not
supported. The sidebar is an untrusted presentation surface relative to the host:
messages are validated and provider/project text is rendered as text, not HTML.
The extension inherits existing model and application-guard limitations.

## Verification and evidence

See [server](../../agent/server.py), [extension](../../vscode/src/extension.ts),
[protocol tests](../../tests/test_server.py) and [extension tests](../../vscode/test/).
The [editor architecture](../vscode-extension.md) defines the protocol operations.

## Revisit when

Third-party editor clients require a stable public service, browser-only support
is required, or measured demand justifies streaming/multiplexed shared sessions.
Do not turn private stdio assumptions into a network API without a new boundary.
