# Changelog

## [1.0.5] — 2026-08-19

perf: eliminate poll-driven re-renders and harden dev tooling

- 5 files changed, 876 insertions(+), 1 deletion(-) · LICENSE, README.md, frontend/index.html, frontend/src/ThemeContext.tsx, frontend/src/ThemePanel.tsx

## [1.0.4] — 2026-08-19

perf: eliminate poll-driven re-renders and harden dev tooling

- 12 files changed, 1315 insertions(+), 104 deletions(-) · CHANGELOG.md, VERSION, backend/app/main.py, frontend/package.json, frontend/src/App.tsx, frontend/src/DatastoresView.tsx, …

## [1.0.3] — 2026-08-19

ui: collapsible vMotion panel with full-screen help modal

- staged changes

## [1.0.2] — 2026-08-19

chore: add auto-version bump hook system

- 6 files changed, 654 insertions(+), 181 deletions(-) · backend/app/main.py, frontend/src/App.tsx, frontend/src/ChangelogModal.tsx, frontend/src/MigrationAccessPanel.tsx, frontend/src/sort.ts, frontend/src/styles.css, …

## [1.0.1] — 2026-08-19

release: vFleet v1.0.0 — initial major release

- 2 files changed, 322 insertions(+) · scripts/bump-version.py, scripts/install-hooks.sh

All notable changes to vFleet are documented here.

---

## [1.0.0] — 2026-08-19

First major release. vFleet is a self-hosted local web console for VMware vCenter / ESXi that lets operators browse inventory, manage VMs, and reclaim idle labs — all through a browser tab, with no cloud relay required.

### Core infrastructure

- **FastAPI backend** (`backend/app/`) with uvicorn, pydantic v2 settings, and pyvmomi 8 for VMware SOAP/PropertyCollector calls.
- **React 18 + TypeScript + Vite** frontend (`frontend/src/`) with recharts for utilization charts.
- **SQLite local store** (`data/vfleet.db`) caches inventory, job queue, and staged uploads so the console keeps working when the VPN drops.
- **Background relay worker** retries queued jobs against vCenter with backoff; reattaches in-flight vCenter tasks after reconnect.
- **Demo mode** serves synthetic lab inventory when no `.env` credentials are present, so all UI features are explorable immediately.

### Authentication & connection

- vCenter session management via pyvmomi SOAP; credentials stored in local `.env`.
- **Connect to vCenter** modal: test connection without committing, save credentials to `.env`, stay in demo, disconnect, or forget saved credentials.
- `APP_MODE=auto` switches between demo and live vCenter automatically based on credential presence.
- Optional `UI_TOKEN` gates the API for deployments beyond localhost.

### Inventory & views

- **Overview** — cluster-level summary: host count, VM count, power state distribution, vCPU and RAM totals.
- **Hosts** — per-host list with cluster, CPU/memory usage bars, VM count, and connection state.
- **Machines** — full VM table with per-row power state, guest OS, Tools status, IP, vCPU, RAM, disk, current host/cluster. Multi-column sort, free-text search, owner and cluster filters, CSV export.
- **Owners** — aggregated resource report grouped by VM name prefix or vCenter custom field (`Owner`, `User`, `CreatedBy`). Shows VM count, on/off/suspended, vCPU, RAM, idle candidates. CSV export per owner.
- **Monitoring** — CPU and memory utilization charts (recharts) per host or owner, driven by `summary.quickStats`.
- **Datastores** — capacity and free-space table, file browser, mkdir, file delete. Last listing cached when vCenter is unreachable.
- **Jobs** — live job queue showing queued, active, and completed operations with status and error detail.
- **Reclaim** — scored list of idle powered-on VMs (low CPU/RAM utilization, stale console/power activity, large reservation). Bulk guest-shutdown, hard power-off, or delete-from-disk with confirm.

### VM operations

- **Power actions**: guest shutdown, guest reboot, hard power off, reset, power on, suspend — queued on disk and retried on reconnect. Guest shutdown/reboot require VMware Tools.
- **Delete from disk** (`destroy`): powers off first, irreversible, requires explicit confirm.
- **Clone from template**: clones a VM from a template already on the remote side (no disk copy over WAN). Resumes by reattaching the vCenter task.
- **Host migrate (vMotion)**: move selected VMs to another host in the same cluster. 50-VM relay cap; extra VMs deferred to a second batch.
- **Storage vMotion / Thick→Thin**: convert disk provisioning type via `RelocateVM`; same datastore is valid.
- **Batch actions**: multi-select VMs with configurable batch limit (`BATCH_LIMIT`); `BatchLimitDialog` prevents accidental mass operations.

### Datastores & uploads

- **ISO / OVA upload**: file staged locally first so a browser refresh cannot lose the in-flight transfer. Relay pushes via Content Library (resumable at byte offset) with fallback to datastore HTTP PUT + retry.

### Reclaim heuristics (tunable via `.env`)

- `CPU_IDLE_PCT` (default 8%) and `MEMORY_IDLE_PCT` (default 20%) thresholds.
- `IDLE_DAYS` (default 14) inactivity window from last console ticket, power events, and reconfigure/migrate events.
- Scores higher for large reservations (≥ 8 GiB or ≥ 4 vCPU while idle).

### Migration access

- `MigrationAccessPanel` can assign a `vFleet-Migrate` role to the current login on a cluster or globally if the account has `Authorization.ModifyPermissions`. Requires explicit confirm; does not grant Administrator.

### Versioning system

- `VERSION` file at repo root (`1.0.0`) is the single source of truth.
- `frontend/src/version.ts` exports `APP_VERSION` consumed by the React UI.
- `backend/app/main.py` declares `APP_VERSION = "1.0.0"` used by FastAPI metadata and the new `/api/version` endpoint.
- `frontend/package.json` bumped to `1.0.0`.
- **Clickable version badge** in the sidebar brand area (accent-colored pill, links to the releases page, tooltip shows full version string).

### Developer experience

- `scripts/dev.sh` — starts API on `:8081` + Vite HMR on `:5173`.
- `scripts/run.sh` — production build; serves UI + API from a single port at `:8081`.
- `backend/tests/` — 8 pytest test files covering store, relay, grouping, reclaim, metrics, and API routes.
- `.env.example` credential stub; `.gitignore` excludes `.env`, `data/`, and `frontend/dist/`.

---

*Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/). Versions follow [Semantic Versioning](https://semver.org/).*
