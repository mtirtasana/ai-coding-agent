# ADR-0011: Report measured usage and independently checked fixtures

- Status: Accepted
- Recorded: 2026-10-20
- Scope: observability and evaluation
- Supersedes: none

## Context

Token efficiency and coding accuracy are separate outcomes. Character counts are
cheap to obtain but differ from provider tokenization. A model's claim that its
edit is correct cannot serve as an independent correctness measurement. Raw
conversation logging can expose project content.

## Decision

Record provider usage only when reported, retaining unknowns as `None`. Report
character selection statistics separately. Emit standard metadata events without
raw prompts/files. Offer opt-in fixture evaluation with fresh workspaces,
independent checks after changed-code runs, and explicit `passed`, `failed` and
`not_run` states. Report observed usage sums and contributing response counts.
Require `--live` for endpoint calls and `--allow-exec` for code-changing evaluation.

## Alternatives considered

Estimating tokens from a fixed character ratio is convenient but can masquerade
as a measurement. Model self-grading is cheap but not independent. Full event
sourcing could support replay but needs privacy, retention and recovery design.
A broad external coding benchmark would improve external validity but exceeds
the current small harness.

## Consequences

Missing observations remain visible. Metadata cannot reconstruct a session, and
custom event payloads are not automatically redacted. Arithmetic fixtures and
keyword-based explanation checks provide limited accuracy evidence. Baseline and
workflow differ in tool bundles, so their difference does not isolate one
algorithm. The outside-workspace checker is not protected from arbitrary host
code by OS isolation. No live-model improvement is established by unit tests.

## Verification and evidence

See [events.py](../../agent/adapters/events.py),
[evaluate.py](../../agent/evaluate.py), [test_usage.py](../../tests/test_usage.py),
[test_evaluation.py](../../tests/test_evaluation.py) and the
[evaluation methodology](../components.md#14-validation-hypotheses-and-limits-of-inference).

## Revisit when

Making quantitative quality/cost claims, comparing models, or publishing results.
Use representative tasks, repeated paired trials, independent checks, uncertainty
estimates and explicit unavailable-run/usage-coverage reporting first.
