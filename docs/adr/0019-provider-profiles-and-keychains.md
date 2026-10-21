# ADR-0019: Named CLI provider profiles with OS-keychain references

- Status: Accepted
- Recorded: 2026-10-21
- Scope: CLI connection configuration; existing provider adapters
- Supersedes: none

## Context

Repeated provider flags make switching endpoints cumbersome. Saving flags alone
would encourage putting API keys into project files. The editor already has a
separate settings and SecretStorage lifecycle.

## Decision

Add `/connect`, `/providers`, `--connect` and `--profile`. Save nonsecret versioned
profile metadata outside the workspace and opaque references to keys in supported
OS vault backends through optional `keyring`. Reject plaintext keyring backends;
keep existing environment-variable configuration available for headless hosts.
Use lock/revision checks and atomic replacement for metadata. Construct a candidate
provider before committing configuration or changing the active connection. Make
no inference request during setup. Successful switches create a new conversation
in Plan, while historical usage stays available. Keep VS Code SecretStorage.

## Alternatives and consequences

Environment-only configuration remains useful but offers no named profiles.
Plaintext JSON keys would simplify deployment at an unacceptable storage cost.
Sharing CLI profiles with editor secrets would couple different host lifecycles.
The optional vault dependency requires a supported, unlocked OS service; Ollama
does not require it. Keychain and JSON are not one transaction: rollback/cleanup
is best effort and process death can leave orphaned entries. Profile names and
endpoints remain local sensitive metadata even though keys are separate.

## Verification and revisit conditions

See [connection adapters](../../agent/adapters/connections.py),
[tests](../../tests/test_connections_usage.py), and the
[operational guide](../providers-and-usage.md). Fake vault tests establish
application semantics; real vault availability requires per-host checks.
Revisit if more provider types, centrally managed configuration or explicit
editor profile import is required.
