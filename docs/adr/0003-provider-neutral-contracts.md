# ADR-0003: Own the message and result contracts

- Status: Accepted
- Recorded: 2026-10-20
- Scope: domain data and provider translation
- Supersedes: none

## Context

Responses, Chat Completions and Ollama use different representations for messages,
function calls and usage. Passing SDK objects through the engine would make
provider-specific assumptions part of every extension and test.

## Decision

Use repository-owned dataclasses for messages, calls, tool specifications,
results, context statistics and usage. Keep call IDs and result references
explicit. Have provider adapters translate to/from these contracts. Use Python
`Protocol` for ports rather than a required inheritance hierarchy.

Unknown token usage is `None`; zero is retained only when actually reported.
Structured tool reports remain strings within `ToolResult.content`, with an
outer success envelope supplied by the engine. The registry currently validates
only string-valued arguments and zero-argument objects.

## Alternatives considered

Using one provider's SDK types simplifies that provider but burdens others with
its protocol. Untyped dictionaries are flexible but make contracts harder to
inspect. A comprehensive schema library offers stronger runtime validation at
the cost of another dependency and more configuration.

## Consequences

The common representation makes small test doubles practical. Adapters must
maintain call identity, validate assumptions and normalize usage. Frozen fields
are not deep immutability; nested schemas remain mutable. Protocols and hints
provide no automatic runtime validation. Streaming, images and provider-specific
reasoning items are not represented by this contract.

## Verification and evidence

See [types.py](../../agent/core/types.py), [ports.py](../../agent/core/ports.py),
[test_usage.py](../../tests/test_usage.py) and
[test_compatible.py](../../tests/test_compatible.py). Python documents
[Protocol semantics](https://docs.python.org/3.11/library/typing.html#typing.Protocol).

## Revisit when

New providers require information that cannot be translated faithfully, malformed
responses become a significant failure source, or non-string tools are added.
Change the contract and its validation together, retaining compatibility where
practical rather than silently dropping required fields.
