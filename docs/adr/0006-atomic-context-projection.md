# ADR-0006: Project complete history units within a character budget

- Status: Accepted
- Recorded: 2026-10-20
- Scope: model request context
- Supersedes: none
- Partially superseded by: [ADR-0024](0024-token-efficient-context.md) for optional compaction and token guards; `recent` remains the default.

## Context

Repeatedly transmitting the complete session grows request size. Cutting arbitrary
characters can separate a tool result from its call or remove essential rules.
Exact tokenizer support differs by provider, and summarization introduces another
potential source of information loss and model cost.

## Decision

Use a deterministic `ContextBuilder` with `recent` and `full` modes. Validate
history groups, pin the system prefix and original/latest requests, and retain
complete recent rounds and optional older complete turns. The newest active
round must fit. Stop with `ContextLimitError` if mandatory context exceeds the
budget. Preserve full source history in memory. Use the documented approximate
character function, not an invented token estimate.

## Alternatives considered

Full history is available as a baseline but grows indefinitely. Arbitrary slicing
risks invalid protocol groups. Summaries, semantic retrieval and token-aware
allocation can be useful but need separate quality and provider-calibration
measurements. The recency algorithm is not an optimal relevance selector.

## Consequences

Selection is inspectable, repeatable for the same inputs and requires no extra
model request. Useful old facts can be omitted, large mandatory messages can stop
a run, and retained memory still grows. Character reduction is not a claim of
proportional token or cost savings. Research on long-context positional effects
motivates experiments, not proof of this policy's quality.

## Verification and evidence

See [context.py](../../agent/core/context.py),
[test_context.py](../../tests/test_context.py) and the
[context algorithm](../components.md#6-context-selection).
[Lost in the Middle](https://arxiv.org/abs/2307.03172) provides relevant external
research with different models and tasks.

## Revisit when

Controlled experiments show lost context harms accuracy, histories cause memory
pressure, or a supported provider requires exact token budgeting. Compare
relevance/summary strategies with independent accuracy checks and usage coverage.
