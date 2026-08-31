# vFleet agent guidance

## Model routing (set manually in Codex UI)

This repository is a local vCenter/ESXi operator console, not a low-risk CRUD
application. Select the model and reasoning effort in the Codex UI before
starting work. This file is routing guidance only: it cannot change the active
model or effort during a task.

### Default

Use **`gpt-5.6-sol` at `high` reasoning effort** as the project default.

This is the appropriate baseline for tasks that touch backend or frontend
behavior, API contracts, data models, the relay/job queue, vCenter adapters,
or tests. The default favors careful repository understanding and validation
over quick edits. Before changing behavior, trace the request through the
Pydantic models, FastAPI route, relay/store, adapter, frontend API client, and
relevant tests as applicable.

### Escalate before implementation

Switch to **`gpt-5.6-sol` at `max` reasoning effort** when a task is risky,
ambiguous, cross-cutting, or production-impacting. Escalation is required for:

- Authentication, `UI_TOKEN`, CORS/bind-address changes, credential storage or
  `.env` handling, TLS validation, console tickets, or access-control changes.
- vCenter/ESXi SOAP or REST behavior, privilege/role assignment, DRS overrides,
  power operations, deletion, cloning, migrations, datastore browsing/deletion,
  uploads, path handling, retry/idempotency, or task reattachment.
- SQLite schema or data migrations, job-queue state transitions, cache/stale
  data semantics, concurrency, recovery after disconnect, or file staging.
- Changes spanning the FastAPI models/routes, relay, adapters, and React client;
  broad refactors; production run/deployment paths; dependency upgrades; or a
  failing/ambiguous test whose cause is not already isolated.
- Any change that could affect a live vCenter inventory, VM availability,
  operator credentials, stored data, or irreversible remote actions.

At this setting, explicitly identify failure modes, preserve confirmation and
least-privilege guardrails, and run the closest relevant tests plus frontend
typecheck/build when the UI contract changes. Prefer a small, reviewable change
over speculative architectural cleanup.

### Lighter setting: narrow exceptions only

Use **`gpt-5.6-luna` at `low` reasoning effort** only when the work is clearly
bounded, has no behavior/security effect, and can be validated locally. Good
examples are README/AGENT documentation, comments, changelog or version-text
updates, repository indexing, simple copy/style-only changes, and triaging a
test failure after its cause and a safe next action are already known.

Do not use the lighter setting for code that parses inputs, calls vCenter,
touches credentials, jobs, data, schemas, tests that encode safety behavior, or
anything with an uncertain blast radius. If a task begins in the lighter tier
and crosses one of those boundaries, stop and switch to the default or
escalation setting before editing.

## Project guardrails

- Treat every vCenter credential, console ticket, datastore path, and inventory
  record as sensitive. Do not expose secrets in output, logs, fixtures, or
  commits. Keep `.env` untracked.
- Preserve the existing explicit `confirm` checks on destructive or
  infrastructure-changing operations. Do not make defaults more permissive.
- Keep the local-first deployment posture: `127.0.0.1` is the default bind and
  a non-empty `UI_TOKEN` is required before exposing the UI beyond localhost.
- The relay is the boundary for remote vCenter work. Maintain idempotency,
  retry/backoff, persistent job state, and reconnect/task-reattachment
  semantics; do not move remote actions into request handlers casually.
- Tests use the demo adapter and an isolated temporary data directory. They do
  not prove behavior against a live vCenter/ESXi environment; plan extra review
  and operator validation for adapter or production-path changes.

## Why this routing is intentionally conservative

vFleet is a roughly 70 tracked-file Python/TypeScript application with a
FastAPI backend, React/Vite frontend, SQLite store, persistent relay, and both
pyVmomi SOAP and vCenter REST/HTTP transfer paths. Its modules are coupled by
shared Pydantic models and job payloads. It handles credentials and optional
TLS bypass, caches inventory during VPN failures, stages uploads, and can power
off or destroy VMs, migrate/clone them, modify DRS and permissions, and delete
datastore files.

The repository has useful guardrails and 78 demo/unit tests across API,
session, relay, migration, transfer, and destructive-action flows. However,
there is no checked-in CI configuration or real-vCenter integration suite.
Recent fixes addressed read-only upload enforcement, streaming upload and saved
credentials, and SQLite metric-index migration ordering. Those facts make
careful reasoning and validation more valuable than raw throughput.

## Reassess

Review this routing by **2026-11-30**, and sooner after a major architecture
change, repeated regressions, new deployment path, new live-vCenter coverage,
or a substantial change in the Codex models available in the UI. Re-test the
default and escalation settings on representative safe tasks rather than
assuming a newer or higher-effort model is automatically better.

