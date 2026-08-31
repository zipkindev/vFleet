# vFleet project index and model-routing evidence

## Snapshot

| Area | Evidence | Routing implication |
| --- | --- | --- |
| Size and languages | About 70 tracked files; FastAPI/Python backend and React/Vite/TypeScript frontend. The active source tree is about 16.9k lines excluding virtual environments and `node_modules`. | Small enough to inspect, but not simple: understand both layers before contract changes. |
| Architecture | FastAPI routes and Pydantic models feed a React API client; a SQLite-backed persistent relay executes vCenter work with retries, idempotency, cached inventory, and task reattachment. | Use `gpt-5.6-sol` / `high` by default; cross-layer changes need deliberate tracing and tests. |
| External integration | pyVmomi SOAP plus vCenter REST/HTTP paths cover inventory, power, console tickets, cloning, migrations, DRS, permissions, datastore operations, and transfers. | Use `gpt-5.6-sol` / `max` for adapter, privilege, migration, transfer, and remote-action work. |
| Sensitive data and security | UI login persists vCenter credentials in a local `.env`; the API supports an optional UI token, CORS, console tickets, and configurable TLS verification (`VCENTER_INSECURE` defaults true). | Escalate authentication, secrets, network exposure, CORS, TLS, and console changes to `sol` / `max`. |
| Destructive/operational blast radius | The product can start, stop, reset, suspend, and destroy VMs; clone/migrate VMs; alter DRS; grant a migration role; upload/delete datastore files. Explicit `confirm` checks and action allowlists exist. | Treat behavior changes as production-impacting; preserve confirmations and least privilege; use `sol` / `max` when safety guarantees change. |
| Data and resilience | SQLite holds jobs, cached inventory/catalog/browse data, metrics, and staging metadata/files. The relay retries failures and requeues orphaned jobs. | Escalate schema, storage, cache, concurrency, recovery, and retry/idempotency work to `sol` / `max`. |
| Test and CI state | 78 backend demo/unit tests span APIs, sessions, jobs, uploads, migrations, and safety checks. On 2026-08-30, 77 passed and one time-sensitive metrics-history test failed because a fixed 2026-08-19 fixture fell outside the current query window. Frontend `npm run typecheck` and `npm run build` passed; build warns of a 686.62 kB JS chunk. No checked-in CI workflow or live-vCenter integration suite was found. | Default to careful reasoning; do not interpret demo tests as live-infrastructure proof. Investigating/fixing the failing test needs at least `sol` / `high`, and `sol` / `max` if the time/data semantics are unclear. |
| Documentation and conventions | README documents operator scope, least-privilege access, local bind, token use, demo/production scripts, data location, and irreversible actions. `.gitignore` excludes `.env`, virtual environments, build output, and local data. No existing AGENTS/Cursor rules or project index was found before this addition. | Follow README and this file; light routing is limited to purely editorial/indexing work. |
| Regression signals | Recent commits fixed read-only datastore uploads/PUT-via-ESXi behavior, streaming uploads and retained saved passwords, and schema-index migration ordering. | These are evidence of sensitive boundary and migration regressions; use `sol` / `max` on comparable changes. |

## Selected model routing

| Route | Codex UI selection | Use for | Trade-off |
| --- | --- | --- | --- |
| Default | **`gpt-5.6-sol` / `high`** | Normal implementation, debugging, reviews, API/UI contracts, models, relay/store, adapters, and tests. | More latency than balanced/light models in exchange for stronger repository reasoning and verification. |
| Escalation | **`gpt-5.6-sol` / `max`** | Security, credentials, permissions, destructive operations, vCenter behavior, uploads, migration, persistent data, recovery, production/deployment, broad refactors, unclear failures. | Highest routine cost/latency; reserve for work where an incorrect change could disrupt infrastructure, data, or access. |
| Light exception | **`gpt-5.6-luna` / `low`** | Documentation, indexing, comments, version/changelog text, explicitly nonfunctional copy/style edits, and already-isolated test triage. | Fast and efficient, but insufficient for uncertain behavior, security, external-system, or cross-layer work. |

OpenAI's current model guidance describes `gpt-5.6-sol` as the flagship-capability
choice, `gpt-5.6-terra` as the lower-price balance, and `gpt-5.6-luna` for
efficient high-volume work. It recommends intentional reasoning selection,
using `high`/ `xhigh` when there is a demonstrated quality gain and reserving
`max` for the hardest quality-first work. This project uses `high` as its
conservative normal baseline and `max` for the defined high-blast-radius
triggers, not as an indiscriminate default.

## Reassessment trigger

Reassess by **2026-11-30** or after a major architectural change, repeated
regressions, a new deployment path, materially improved integration/CI
coverage, or a substantial change in available Codex models. Model selection is
manual in the Codex UI; these files provide routing guidance and do not switch
the active model automatically.

