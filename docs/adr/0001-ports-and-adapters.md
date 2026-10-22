# ADR-0001: Separate the application through ports and adapters

- Status: Accepted
- Recorded: 2026-10-20
- Scope: application structure
- Supersedes: none

## Context

The coding workflow must support cloud APIs, local models, offline tests and
multiple interfaces without placing SDK or terminal dependencies in the core.
Changes in provider wire formats should have a local point of adaptation.

## Decision

Define `ModelProvider`, `ContextBuilder`, `ToolExecutor` and `EventSink` as ports.
Keep orchestration and policies in `agent/core`; implement external interactions
in adapters. Assemble concrete objects in `agent/cli.py`, the composition root.
Dependencies are injected through constructors rather than discovered globally.

This implements the separation described by
[Cockburn's ports-and-adapters pattern](https://alistair.cockburn.us/hexagonal-architecture).
It does not require six ports, a framework or a database.

## Alternatives considered

A single script minimizes initial indirection but couples provider changes to the
workflow. A framework-owned orchestration graph offers more built-in facilities
but would introduce runtime contracts and dependencies beyond current needs.
Abstract base classes were unnecessary for these small structurally typed ports.

## Consequences

The core can be exercised with deterministic test doubles and no SDK. Adding a
provider normally changes an adapter and CLI selection, not engine logic.
Translation code and explicit wiring require maintenance. A common port exposes
only shared capabilities; advanced provider features may need contract changes.
This structure alone guarantees neither interchangeability nor coding quality.

## Verification and evidence

See [ports](../../agent/core/ports.py), [CLI](../../agent/cli.py) and
[test_architecture.py](../../tests/test_architecture.py). The
[component map](../components.md#2-system-decomposition) distinguishes runtime
calls from source dependencies. [ADR-0003](0003-provider-neutral-contracts.md)
records the shared data contracts.

## Revisit when

New interfaces require provider-specific behavior in the core, wiring becomes
repetitive across actual entry points, or measured operational needs justify a
framework. Evaluate those needs before introducing a dependency-injection library.
