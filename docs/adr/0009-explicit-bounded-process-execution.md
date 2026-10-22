# ADR-0009: Execute explicit host commands with bounded capture

- Status: Accepted
- Recorded: 2026-10-20
- Scope: local verification processes
- Supersedes: none

## Context

The agent needs actual test/scanner results, but accepting arbitrary commands
from model arguments would expand its authority. Processes can hang, generate
large output or leave descendants. A clean exit alone is insufficient if the
output reader failed and concealed relevant observations.

## Decision

Require `--allow-exec` and explicit host-configured commands. Expose zero-argument
tools; parse CLI strings with POSIX `shlex.split` and execute fixed argv with
`shell=False`, workspace cwd and noninteractive stdin. Inherit the host environment.
Bound retained output while draining pipes. Apply deadlines and best-effort
process-tree cleanup. Require exit zero, no timeout and successful capture for
`passed=True`; expose `capture_error` and stop a running process on reader failure.

The Windows cleanup utility is resolved under trusted `SystemRoot`, not the
workspace/PATH. The general command itself still uses normal host executable
resolution. No package-installation or security-upgrade tool is exposed.

## Alternatives considered

Model-generated shell strings are flexible but grant substantially more command
selection authority. Output slicing after `communicate()` does not bound capture
memory. Containers can add OS isolation but need a deployment contract; they are
not implied by a timeout or an argv list.

## Consequences

Command selection is explicit and output retention bounded. Commands can still
write files, read credentials or access the network with host authority. Deadlines
are not exact total-runtime bounds because startup/cleanup add time. Escaped
descendants and platform differences limit cleanup. POSIX quoting requires care
for Windows paths. Output truncation can hide findings even when the process
status is available.

## Verification and evidence

See [execution.py](../../agent/adapters/execution.py),
[test_execution.py](../../tests/test_execution.py), and
[Python subprocess security semantics](https://docs.python.org/3.11/library/subprocess.html#security-considerations).
The tested runner is not an OS sandbox.

## Revisit when

Untrusted workloads, strict end-to-end budgets or stronger platform guarantees
become requirements. Define isolation and executable-selection rules, and obtain
native-platform evidence rather than inferring it from mocked cleanup tests.
