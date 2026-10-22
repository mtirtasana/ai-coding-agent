# Token consumption and context controls

Batch inspection, exact archived recovery, mandatory path observations and
completed-task sealing extend these controls. See [context artifacts](context-artifacts.md)
for command/API contracts, encryption opt-in and storage limits.

Each model call sends the selected conversation and tool definitions. A large
file read or command log therefore costs input again on later calls while it
remains selected. Provider prompt caching may discount repeated input; it does
not make selected context disappear. The local SQLite usage database records
counts and cannot itself reduce model input.

## Default behavior

Use `search_text` and `read_file_range` to retrieve relevant code. Range reads
retain the full-file SHA-256 used by optimistic editing checks. They limit model
input, while the host still reads and hashes the complete eligible file.

VS Code replacement previews use a unified diff with three surrounding lines,
rather than sending both complete files. Preview generation leaves buffers and
edit-approval checks unchanged. Previews remain bounded at 20,000 characters,
with an explicit truncation flag.

Test, lint and fixer results preserve execution status, capture status, exit
codes, timeouts and cancellation. Long successful logs expose their last 1,200
characters; long failed logs expose the first and last 3,000 characters. These
are explicitly labeled excerpts. `read_check_output(output_id, start_line,
end_line)` retrieves captured lines without rerunning the command, using the
same 200-line/12,000-character range limits as file reads. Captured truncation
and excerpt omission have separate flags. Security and dependency audit output
remain verbatim, including findings from commands returning zero.

The command runner and VS Code check panel retain their full bounded capture.
The model's retrieval store holds up to eight excerpts' original captures and
one million characters per tool registry/run. Old log references may expire;
retrieval returns an explicit error rather than rerunning a check. Commands that
produce more than the capture limit still report `output_truncated`.

## Optional history compaction

```bash
coding-agent --profile work --context-mode compact
```

`recent` remains the default; `full` is the uncompressed baseline. In `compact`,
older successful `file_snapshot` and `read_file_range` results with at least
4,000 source characters become historical references containing the path, hash,
range metadata and a request to read current source before relying on omitted
details. The latest two rounds remain intact. No extra model call creates these
references, and the complete original history remains available for recovery.

Compaction preserves tool calls and their result IDs, assistant decisions,
user requests, project instructions, accepted plans and verification results.
It does not summarize arbitrary source text into instructions. A historical
hash never proves the current file is unchanged; existing read/write guards and
host-owned verification remain authoritative. Edit arguments, arbitrary tool
output and failed reads are not compacted.

Reading omitted details again can offset savings or affect reasoning. Enable
this mode per project after evaluation rather than assuming it improves every
task. It does not bound the original history's memory or recovery-file size.
Changing older input can also reduce provider prompt-cache hits; compare cached
input alongside total input before drawing conclusions about cost.

## Token estimates and budgets

```bash
# Both budgets are optional; the existing character limit still applies.
coding-agent --profile work --context-mode compact \
  --max-context-tokens 32000 --max-run-tokens 120000 \
  --max-output-tokens 4096

# Optional model-aware estimates for recognized OpenAI models:
pip install -e '.[tokens]'
```

`--max-context-tokens` covers estimated input **plus the configured output
reserve**, not input alone. The selector removes optional complete history
units until they fit. Mandatory rules, pinned requests and the latest active
round are never silently truncated; a budget that cannot fit them stops before
the request. `full` mode stops instead of dropping history.

The optional `tiktoken` adapter uses a recognized OpenAI model's encoding and a
framing margin. Its first use may download public encoding data in a background
daemon, shared per encoding (at most eight loads per process). A request waits at
most 50 ms for optional assets and otherwise uses the byte fallback; background
loading makes no inference request and failures are not automatically retried.
Other providers,
unknown models, or unavailable tokenizers use UTF-8 byte length plus framing
allowance as a conservative estimate. Wire formats and hidden provider
framing differ, so neither estimator is an exact bill or universal context-limit
guarantee. Estimates include structure, tool schemas and names, and non-ASCII
text; the existing character metric remains separately labeled.

The core accepts a `TokenEstimator` port via `Agent(token_estimator=...)`. An
adapter implements `estimate(messages, tools) -> TokenEstimate(tokens, method)`;
the core has no tokenizer or provider SDK dependency. Frontends select the model
adapter. This keeps new provider estimators independent of context selection.

`--max-run-tokens` resets for each submitted task. Before each call it reserves
estimated input and the maximum output. Afterwards it charges provider-reported
input/output counts where available and estimates for missing fields. Cached
input is part of input, not subtracted a second time. The next call is refused
when it cannot fit; already accepted edits remain. Actual provider usage can
exceed estimates, so use provider-side spending controls for hard monetary caps.
No budget triggers an automatic retry or truncates a tool call for execution.

Request metrics and JSONL events expose `estimated_input_tokens` and the estimate
method. The terminal summary labels estimates separately. SQLite, `/stats` and
the usage panel continue to contain provider-reported values only; unknown
counts stay unknown. Both budgets are disabled unless configured.

In VS Code configure `genericAgent.contextMode`, `genericAgent.maxContextTokens`
and `genericAgent.maxRunTokens`; zero disables a token budget. Changes restart
the backend and invalidate existing verification, following other settings.
The managed runtime uses the byte fallback unless its optional tokenizer is
separately installed. Budget guards apply to agent tasks; inline completion
uses its existing 8,000-character input and 256-token output caps.

## Inline completions

Identical requests share one pending inference. The most recent successful
result can be reused for ten seconds only with the same workspace generation,
document URI/version, cursor offset, context, language and provider/model
settings. Cache entries stay in process memory and are cleared on stop, new
conversation, provider/configuration changes and disposal. Only one entry is
retained per workspace. Stale responses still undergo the existing cursor and
version checks.

Cancelling one consumer leaves other consumers of the same request active.
Cancelling the last consumer disposes an in-flight completion worker; the next
request starts a fresh worker. Disposing the local worker cannot guarantee that
the remote provider stops generating or charging. The sidebar displays local
request, reuse and discard/cancellation counts separately from token accounting.
They measure editor behavior, not billable tokens saved.

## Validation and measured limits

The deterministic three-task coding evaluation uses large-file, multi-file and
dependency-context fixtures with independent post-edit verification. In the
local scripted comparison, cumulative selected context decreased from 288,278
to 221,098 characters (23.3%) for `compact` versus `recent`, with all three tasks
passing. Scripted models exercise transport and edit mechanics, not model
intelligence; these numbers are neither token estimates nor live accuracy proof.

Run paired live evaluations explicitly with the same provider profiles:

```bash
coding-agent-eval --profiles profiles.json --live --allow-exec --compare-reads \
  --context-mode recent --output recent.json
coding-agent-eval --profiles profiles.json --live --allow-exec --compare-reads \
  --context-mode compact --output compact.json
```

Compare matching tasks and read strategies: independent pass rates, model
calls, full provider input/output coverage, cached input, latency and cumulative
context characters. Repeat runs to account for model variance. Savings count
only alongside acceptable task accuracy. Missing usage is not zero.

The decision and tradeoffs are recorded in [ADR-0024](adr/0024-token-efficient-context.md).
