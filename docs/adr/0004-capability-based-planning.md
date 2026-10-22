# ADR-0004: Enforce planning with executable capability policy

- Status: Accepted
- Recorded: 2026-10-20
- Scope: planning and tool authorization
- Supersedes: none

## Context

A prompt asking a model not to modify files is not a reliable execution barrier.
Models can request unadvertised tools, and project instructions may attempt to
contradict the selected mode. Planning needs an enforceable boundary.

## Decision

Wrap the executor with `ModeToolExecutor`. In `plan`, advertise only tools marked
`read_only=True` and reject calls outside that filtered set at execution time.
In `implement`, expose the configured registry. Separately gate file edits by
`--allow-write` and configured processes by `--allow-exec`. Processes are not
classified as read-only, even when a particular scanner normally reads files.

Accepting a plan changes prompt context and mode; it grants no new host permission
and starts no task implicitly. Custom tool authors remain responsible for honest
`read_only` declarations.

## Alternatives considered

Prompt-only planning is simpler but cannot block a forbidden call. Running all
planning in an isolated read-only OS environment could provide a stronger
boundary, but requires deployment/isolation work absent from this project.
Requiring manual approval for every tool would add interaction beyond the
current explicit capability configuration.

## Consequences

Invented writes/process calls are denied in planning, independently of model
cooperation. A falsely classified custom handler can violate the intended policy.
Planning cannot execute tests or scanners. Host commands in implementation mode
can still write despite `--allow-write` being absent; this wrapper is not an OS
permission system.

## Verification and evidence

See [policy.py](../../agent/core/policy.py), [tools.py](../../agent/adapters/tools.py)
and [test_security.py](../../tests/test_security.py), including malicious project
instruction and invented-command cases.

## Revisit when

Read-only execution of specific tools becomes necessary, or tools come from
untrusted extensions. Define enforceable resource/OS boundaries before expanding
what a declarative flag is expected to guarantee.
