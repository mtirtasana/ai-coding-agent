# Documentation

This documentation describes the generic coding agent implemented in version
0.10.0, including the improved interactive CLI, focused range reads and the VS Code editor adapter on PR #1. The component reference and
ADRs are in English. Earlier operational guides are available in German or
English as indicated below.

## Reading paths

| Goal | Start here | Then read |
| --- | --- | --- |
| Understand the system without reading all source files | [Component reference](components.md) | Its worked example and component map |
| Understand why the system has this structure | [ADR index](adr/README.md) | The decision relevant to your change |
| Use the VS Code extension | [Extension guide](../vscode/README.md) | [Editor architecture](vscode-extension.md) |
| Reduce model input and measure tradeoffs | [Focused reads and context cost](token-efficiency.md) | [ADR-0017](adr/0017-focused-file-reads.md) |
| Run the agent | [Project README](../README.md) | [Interactive terminal](interactive.md) |
| Modify and verify code | [Coding workflow](coding-workflow.md) | [Tests and linting](testing-and-linting.md), [security checks](security-checks.md) |
| Read the existing German architecture guide | [Architecture](architecture.md) | [Component reference](components.md) for the formal model and evidence limits |
| Investigate future persistence | [ADR-0012](adr/0012-defer-persistence-and-caching.md) | [Context and SQLite proposal](context-and-persistence.md), in German |

The component reference explains responsibilities, contracts, algorithms, failure
behavior and scientific interpretation. ADRs record decisions, alternatives,
consequences and conditions that would justify revisiting them. Operational
guides give commands and configuration details.

## What “scientific” means here

We distinguish four kinds of statements: implemented behavior checked against
source code, properties exercised by tests, design hypotheses that still require
experiments, and proposals that are not implemented. Equations define observable
quantities or specify contracts; they are not claims of formal verification.
Research references explain related ideas and do not establish benchmark results
for this repository.

SQLite storage, semantic retrieval, automatic summarization, model-response
caching, OS process isolation and multi-agent orchestration remain unimplemented.
There is no published live-model accuracy or token-saving result in these docs.

## Documentation maintenance

Update the affected component and its evidence links when a contract changes.
Create a new ADR for a significant architectural change; preserve superseded
records and link their replacements. Correct factual errors in existing records,
but do not rewrite an old decision as if the new decision had always applied.
See the [ADR status definitions and template](adr/README.md).
