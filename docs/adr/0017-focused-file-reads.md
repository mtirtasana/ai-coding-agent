# ADR-0017: Prefer bounded file excerpts and measure complete task cost

- Status: Accepted
- Recorded: 2026-10-20
- Scope: file retrieval, prompts and evaluation
- Supersedes: none

## Context

Whole-file snapshots can dominate repeated model input even when a change needs
only one function. Context selection already preserves complete tool rounds,
but does not shrink the content of a retained round. A disk cache alone would
not change what the model receives. Savings must be assessed alongside coding
correctness rather than inferred from a smaller individual response.

## Decision

Offer a read-only, bounded line-range tool alongside existing full reads. Return
a full-file hash for the established targeted-edit workflow. Apply matching
contracts in Python and the editor, retaining their separate disk/buffer hash
ownership. Add range-specific guidance only when the capability is available.
Preserve original history and its complete call/result groups.

Compare full and range strategies with identical fixture content, checks, model
configuration and budgets. Report actual provider usage separately from context
characters and independently check success. Include adverse or unavailable
results rather than treating them as savings.

## Alternatives considered

Lowering the context budget can discard useful evidence without reducing a
single oversized result. AI summaries add calls and omission risk. Model routing
changes the quality/cost comparison. SQLite can support persistence or retrieval
but is unnecessary for bounded reads. Replacing full reads entirely would make
whole-file reasoning harder.

## Consequences

Large-file fixtures send fewer characters with excerpts. Extra tool schema,
guidance and metadata can increase cost for small files. Real models may need
more retrieval calls, so live task-level savings remain unproven. Local adapters
still read the entire eligible file for hashing; this is a model-context change,
not a new large-file or I/O cache implementation.

## Verification and evidence

See [contracts and measured results](../token-efficiency.md),
[range regressions](../../tests/test_ranges.py),
[paired evaluation](../../tests/test_evaluation.py) and
[native editor tests](../../vscode/test/native.cjs).

## Revisit when

Live evaluations reveal excessive extra reads, unacceptable quality regression,
or schema overhead dominates typical workloads. Reconsider retrieval policies,
model-specific budgeting or summarization only with independent evidence.
