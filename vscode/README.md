# Generic Coding Agent for VS Code

An independent sidebar for the Python coding agent, with planning, individually
reviewed edits, configured checks, opt-in inline suggestions and existing editor
diagnostics. Supports desktop VS Code 1.95+ and workspace-host execution in SSH,
WSL and Dev Containers. Browser-only and virtual workspaces are unsupported.

## Install and start

Install the generated `.vsix` using **Extensions: Install from VSIX**. Open a
trusted filesystem workspace. Run **Coding Agent: Configure Provider**, then
**Coding Agent: Set Up / Update Backend**. Setup explicitly creates a managed
environment using your chosen Python 3.11+ interpreter and may download packages.
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

## Settings, privacy and recovery

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
in extension memory. Restarting VS Code loses the conversation. If process cleanup
fails, inspect the workspace host before retrying; arbitrary escaped subprocesses
are outside the isolation guarantee. Changing critical settings stops workers
and invalidates execution permissions.

## Build and test

From this directory, with Node 22+ and Python 3.11+:

```bash
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
