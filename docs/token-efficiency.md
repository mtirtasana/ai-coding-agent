# Focused reads and measured context cost (v0.9)

The agent can retrieve the relevant lines of a file instead of sending its entire
contents to the model. This reduces the size of an individual tool result, which
can also reduce its repeated inclusion in later model requests. It does not
compress history, cache model answers, select a cheaper model or change the
existing character budget. The original conversation remains intact.

## Tool contract and data flow

`read_file_range(path, start_line, end_line)` is read-only and available in both
Plan and Implement, in the CLI and VS Code. Line numbers are JSON integers,
one-based and inclusive. Booleans, floats, numeric strings, missing/extra fields
and integers above JavaScript's safe-integer maximum are rejected. Each request
may span at most **200 lines** and return at most **12,000 Unicode code points**
of file content, including line endings. Exceeding either limit is an error with
guidance to narrow the range; content is not silently cut.

The result is a JSON object inside the existing tool-result content string:

```json
{
  "path": "src/calculator.py",
  "sha256": "<full-file snapshot hash>",
  "start_line": 20,
  "end_line": 25,
  "total_lines": 80,
  "content": "<exact lines 20 through 25, including original endings>"
}
```

VS Code adds `documentVersion`. `path` is canonical and relative to the workspace.
The hash covers the **whole eligible file**, not the excerpt, so it can be passed
to `preview_edit` or `apply_edit`. CLI hashes cover disk bytes; VS Code hashes
cover current editor text, including unsaved changes. Existing stale-hash,
unique-match, review, document-version and Undo behavior remains in force.

CRLF, LF and lone CR delimit logical lines; endings are retained. A final newline
does not create a phantom extra line. An empty file accepts a range starting at
1 and returns empty content with bounds `0, 0` and `total_lines: 0`. A start
beyond EOF otherwise fails. An end beyond EOF is clamped and reported through
the actual `end_line`; the requested span still must fit the 200-line limit.
This logical-line convention differs from VS Code's trailing empty editor line.

The tool still validates and reads the entire eligible source locally to compute
its hash. It retains the existing file-size, UTF-8, NUL and workspace restrictions;
it is not streaming access to previously unsupported large files. There is no
claim that local disk I/O becomes faster.

When the tool is available, the engine adds capability-specific guidance:
search first, read the relevant function and surrounding lines, and expand when
dependencies are unclear. `file_snapshot` and `read_file` remain available for
whole-file reasoning. Smaller context is useful only if it preserves enough
information to produce correct code.

## Paired evaluation

The existing harness now supports:

```bash
# No model requests or fixture execution; rows are not_run.
python -m agent.evaluate --compare-reads --output read-comparison.json

# Explicit live requests and host execution; API charges may apply.
python -m agent.evaluate --compare-reads --profiles profiles.json \
  --live --allow-exec --output read-comparison.json
```

Use the provider-profile format documented in [Coding workflow](coding-workflow.md#opt-in-evaluation).
`--compare-reads` requires the workflow toolset and runs three fresh fixtures for
each of `full` and `ranges`. Both strategies use the same model, task, permission
gates, checks and budget. The full strategy omits only the range tool and its
guidance; it is not the earlier basic-three-tools baseline. Range-mode models
may still choose full reads. Run repeatedly with actual configured models to
assess variability; this harness does not automatically retry or route models.

The fixtures cover a large module, a two-file correction, and a function that
depends on a separate policy helper and constant. External assertions check
behavior on additional inputs; test files and project rules must remain
unchanged. AST comparison also preserves declarations outside the target
functions. Comments and formatting outside those functions are not compared by
the AST check. Temporary projects are not process sandboxes; verification runs
model-generated code with host permissions.

Reports retain existing fields and add `suite` and `read_strategy` per row, plus
`read_comparisons`. Each pair references zero-based result indices and records
whether both variants passed and their relative character reduction. Reduction
is null unless both succeeded; a negative value means increased context.
Rows separately report provider input/output/cached-input tokens with coverage,
model calls, selected-context characters, tool calls and total elapsed time.
Missing token usage stays null. Cached input is a subset of input and is not
assumed free. No price estimate is inferred from characters or partial usage.
Reports omit raw source, prompts, responses, endpoint URLs and raw exceptions.

## Deterministic evidence and its limits

Reproduce the mechanical comparison without a model or credentials:

```bash
python -m unittest discover -s tests -p test_evaluation.py -v
```

On 2026-10-20, scripted model doubles using real tools and independent checks
produced the following cumulative selected-context measurements. Both variants
passed all three fixtures and used the same number of model calls per pair.

| Fixture | Full-read characters | Range-read characters | Reduction | Calls per variant |
| --- | ---: | ---: | ---: | ---: |
| Large file | 71,012 | 43,228 | 39.13% | 7 |
| Two small files | 68,975 | 78,375 | -13.63% | 11 |
| Dependency context | 137,512 | 60,932 | 55.69% | 9 |

The two-small-files case exposes the schema, guidance and metadata overhead:
bounded reads are not a universal cost improvement. These numbers count the
existing character proxy across requests, not wire bytes or tokens. Exact
measurements can vary slightly with process-timing metadata in tool results.
Scripted choices do not establish live-model accuracy, latency, task success rates or
billing savings. No live-provider experiment was run for this change. Smaller
excerpts can also cause additional retrieval calls in real use; compare total
usage for successful tasks, not the size of one request alone.

Source and evidence: [range contract](../agent/adapters/ranges.py),
[Python adapter](../agent/adapters/editing.py),
[editor adapter](../vscode/src/editor.ts),
[evaluation harness](../agent/evaluate.py),
[range tests](../tests/test_ranges.py),
[paired tests](../tests/test_evaluation.py) and
[native editor tests](../vscode/test/native.cjs).
See [ADR-0017](adr/0017-focused-file-reads.md) for the decision and tradeoffs.
