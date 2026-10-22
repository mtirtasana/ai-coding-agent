# ADR-0002: Use a bounded sequential model/tool loop

- Status: Accepted
- Recorded: 2026-10-20
- Scope: run orchestration
- Supersedes: none

## Context

Model responses can contain several tool calls with dependencies between their
side effects. Unbounded iterations risk excessive resource use. The initial
implementation needs understandable execution and reproducible failure tests.

## Decision

Use one synchronous agent. Before each model call, select context; after a tool
response, execute its calls in returned order, append all results, then request
the next response. Stop on a final answer or raise after `max_steps` model calls
(default 15). Step count bounds model calls, not the number of tool calls inside
a response, tokens, money or total wall-clock time.

This is a bounded feedback loop related to model/action interaction studied in
[ReAct](https://arxiv.org/abs/2210.03629), without reproducing its reasoning traces
or experimental claims. The core does not automatically retry a failed run.

## Alternatives considered

Parallel tools could reduce latency for independent reads but need dependency and
conflict handling for writes. Multiple agents add coordination and state-sharing
requirements. A one-shot answer cannot incorporate actual tool observations.
None is ruled out for a future measured use case.

## Consequences

Execution order is explicit, and test doubles can predict observations. Waiting
for each API/process limits throughput. A step-limit error can occur after a
successful edit; side effects are not rolled back. Provider SDKs may retry
network requests independently of the core. Context and process budgets remain
separate controls rather than a single end-to-end resource guarantee.

## Verification and evidence

See [engine.py](../../agent/core/engine.py),
[test_architecture.py](../../tests/test_architecture.py), and
[test_session.py](../../tests/test_session.py). The
[formal model](../components.md#3-formal-model-of-a-run) specifies sequencing.

## Revisit when

Profiling shows waiting on independent operations dominates latency, and a design
can define ordering, cancellation and conflict rules. Add an explicit resource
budget if predictable spend or total runtime becomes a requirement.
