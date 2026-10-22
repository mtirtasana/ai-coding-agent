# Generic Coding Agent for VS Code

Batch reads use dirty buffers and report stability. **Seal Completed Task**,
**Restore Archived Task** and **Read Archived Bytes** manage explicit handoffs.
`genericAgent.persistArtifacts` opts into encrypted local archives after backend
setup; `genericAgent.contextPins` retains selected file observations.
See [context artifacts](../docs/context-artifacts.md) for limits and exact recovery.

Token controls: `genericAgent.contextMode` (`recent`, opt-in `compact`, `full`),
`genericAgent.maxContextTokens` and `genericAgent.maxRunTokens` (zero disables).
See [token efficiency](../docs/token-efficiency.md) for reserve semantics,
retrievable check output, inline reuse and accuracy limits.

An independent sidebar for the Python coding agent, with planning, individually
reviewed edits, configured checks, opt-in inline suggestions and existing editor
diagnostics. Supports desktop VS Code 1.95+ and workspace-host execution in SSH,
WSL and Dev Containers. Browser-only and virtual workspaces are unsupported.

## Install and start

Install the generated `.vsix` using **Extensions: Install from VSIX**. Open a
trusted filesystem workspace. Run **Coding Agent: Configure Provider**, then
**Coding Agent: Set Up / Update Backend**. Setup explicitly creates a managed
environment using your chosen Python 3.11–3.14 interpreter and may download packages.
Python itself is not installed automatically. Ollama requires an existing server
and installed model. Demo needs no model or API key.

Open the Coding Agent activity-bar view. Start in **Plan**, send a task, then
**Accept plan** and send the implementation request. Every agent edit opens a
native diff and waits for **Accept** or **Reject** in the sidebar. Accepted edits
use editor buffers and support Undo. Existing modified files are not saved
implicitly. A failed or cancelled task can leave already accepted edits.

## Checks and suggestions

Configure `genericAgent.checks` with executable/argument arrays:

```json
{
  "genericAgent.checks": {
    "test": {"command": ["python", "-m", "unittest", "discover", "-s", "tests"], "timeout": 60},
    "lint": {"command": ["python", "-m", "ruff", "check", "."]}
  }
}
```

Also supported: `security` and `dependency_audit`. Commands execute on the
workspace host, in the workspace directory, with its environment. They are not
sandboxed. Select the correct project interpreter in each command; the managed
backend environment is not automatically your project's environment. Checks need
session permission and saved workspace buffers. Declined permission/save means
not run. Automatic lint fixing is intentionally unavailable because it could
bypass individual diff review.

Automatic inline completions are disabled initially. **Coding Agent: Toggle
Automatic Completions** opts in for this workspace. **Ctrl+Alt+Space**
(**Cmd+Alt+Space** on macOS) requests one manually. Requests contain at most 6,000
preceding and 2,000 following active-document characters, use a five-second
request deadline and 256 output tokens, and cannot call tools. A separate model
on the same endpoint can be selected with `genericAgent.inline.model`.
Cancellation cannot undo an inference request already received by a provider.

Diagnostics come from your installed language extensions, not new background AI
scans. Use **Explain** or **Propose fix** in the sidebar, or the corresponding
Command Palette commands. Propose fix switches explicitly to implementation and
still requires reviewing each edit.

## Token usage

Run **Coding Agent: Show Token Usage** or use **Refresh usage** in the sidebar.
Choose conversation, today, week, month, rolling 7/30 days, all history or custom
inclusive dates. Provider/profile/model, interface and agent/completion filters
apply on Refresh. Reports work while the agent is busy, without model credentials,
after backend setup. Unknown provider token counts remain unknown, with coverage.

CLI and editor workers share a metadata-only SQLite ledger on the execution host.
SSH/WSL/container storage is separate from desktop-host storage. Prompts, source,
keys and raw endpoints are excluded. Database errors warn and mark reports
incomplete while coding continues. **New** and provider changes start a new usage
conversation without deleting history. The CLI's `/connect` profiles use OS vaults;
the editor continues using **Configure Provider** and SecretStorage.
See [usage architecture, dates and storage locations](../docs/providers-and-usage.md).

## Settings, privacy and recovery

Backend 0.9 introduced bounded `read_file_range` requests over the current editor
buffer, including unsaved text. The full-buffer hash still protects reviewed
edits. Existing full reads remain available. See [focused reads and measured
context cost](../docs/token-efficiency.md). Extension 0.4.0 bundles backend 0.12.0,
including the CLI workflow adapters. After installing it, run
**Coding Agent: Set Up / Update Backend** to prepare its matching backend.

Provider/model/base URL, checks and budgets use `genericAgent.*` settings. API
keys are entered through a password input and kept in VS Code SecretStorage,
associated with provider/endpoint. Do not put credentials in endpoint URLs or
workspace settings. Source context goes to the model endpoint you select. No
telemetry or durable conversation logging is added.

The managed environment lives in VS Code's extension storage on the workspace
host. Setup/update is explicit; package integrity is checked against the bundled
wheel manifest. The Python command is selectable through setup or
`genericAgent.pythonPath`. Missing Python, package installation or proxy failures
are reported without echoing provider request bodies. Retry setup after correcting
the environment. Do not include secrets in diagnostic messages or source files
that you choose to send to a model.

**Stop** cancels pending approvals, stops tracked check processes and discards
late responses. A new worker restores only completed conversation turns retained
in extension memory. Restarting VS Code loses the conversation unless local recovery is enabled. If process cleanup
fails, inspect the workspace host before retrying; arbitrary escaped subprocesses
are outside the isolation guarantee. Changing critical settings stops workers
and invalidates execution permissions.

## Build and test

From this directory, with Node 22+ and Python 3.11–3.14:

```bash
python -m pip install --require-hashes --only-binary=:all: -r backend/runtime-base.txt
npm ci
npm run typecheck
npm run lint
npm test
npm run package
npm run test:integration
```

`AGENT_BUILD_PYTHON` selects the build interpreter. Install the root project into
a test environment and set `AGENT_TEST_PYTHON` to its Python executable for the
real stdio integration test. Native integration tests download VS Code; Linux
needs a display, for example `xvfb-run -a npm run test:integration`. Set
`VSCODE_TEST_VERSION=1.95.3` to test the minimum supported release family.
The VSIX includes only compiled UI/extension assets and the bundled Python wheel;
it contains no project dependencies, keys or `node_modules`.

## Internal production baseline

This build is a release candidate; [live-provider/vault gates](../release/checklist.json)
remain pending. Check results now distinguish stale, unknown and not-run evidence.
`.agentignore` denies all agent file access, while `.gitignore` limits discovery.

Enable user setting `genericAgent.persistSession` to store completed prompts/source
locally, with 30-day inactive expiry. Use **Resume Saved Conversation** or **Delete
Saved Conversation**. Resume enters Plan without replaying tools or retaining plan
acceptance. **Diagnose Setup** and **Export Redacted Diagnostics** expose setup status
without provider requests or source/key data. Backend updates stage a separate
hash-pinned environment and keep the existing one on failure. Install the matching
VSIX, then run **Set Up / Update Backend**. Read the [full production procedure](../docs/production-readiness.md).
