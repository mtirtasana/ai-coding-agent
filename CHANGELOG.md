# Changelog

## 0.12.0 backend / 0.4.0 extension — internal release candidate

- CLI file review is the default with write permission. Trusted automation must
  explicitly use `--no-review-edits`. Direct file replacements are atomic.
- Verification has host-owned passed/failed/stale/not-run/unknown evidence,
  invalidated by input, configuration and editor changes.
- `.gitignore` excludes discovery results; `.agentignore` denies all agent access.
  Credential-path protection applies independently and cannot be overridden.
- Opt-in completed-turn recovery resumes in Plan with renewed plan acceptance.
  Saved conversations expire after 30 inactive days; usage history is retained.
- Provider timeouts, output limits, truncated-response rejection, RPC deadlines,
  bounded failed-write buffers, explicit usage export/pruning and redacted setup
  diagnostics improve failure behavior.
- Backend updates stage a separate environment, install hash-pinned dependencies
  and publish the new runtime only after readiness checks. Protocol 2 requires a
  matching extension/backend.
- Internal artifacts include checksums, runtime locks, dependency inventory,
  migration/rollback instructions and explicit pending live/keychain checks.

Breaking operational changes: unattended CLI writes now require an explicit
review opt-out; Python support is 3.11–3.14; existing editor installations need
**Set Up / Update Backend**. No public registry publication is included.

## 0.11.0 backend / 0.3.0 extension

Named CLI provider profiles using OS-keychain references and shared host-local
metadata-only usage reporting across CLI and editor workers.
