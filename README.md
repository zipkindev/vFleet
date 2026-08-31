# vFleet

A local web console for **your** vCenter or standalone ESXi host: inventory clusters/hosts/VMs, group labs by naming convention, report resource use by owner, and run guarded operator workflows.

This talks to vCenter with credentials **you** put in `.env`. It is an operator tool, not a scanner for systems you do not administer.

## What is possible

| Need | How | Notes |
| --- | --- | --- |
| Authenticate | vSphere session via [pyvmomi](https://github.com/vmware/pyvmomi) (SOAP) | Auto-detects vCenter (`VirtualCenter`) or direct ESXi (`HostAgent`). |
| Clusters, hosts, VMs | PropertyCollector inventory | Names, power state, guest OS, Tools, IP, configured vCPU/RAM, current host/cluster. Cached locally so the UI keeps working if the VPN drops. |
| CPU / memory utilization | `summary.quickStats` | Live usage (MHz / guest memory). This is the same family of numbers the vSphere UI uses, not guest-inside telemetry. |
| Power on / off / reset / suspend | Local job queue → VIM power methods | **Guest shutdown/reboot** needs VMware Tools. Hard power-off does not. Queued on disk; retried after disconnects. |
| Migrate host / convert disks | Local job → `RelocateVM` (vMotion / Storage vMotion) | Default keeps datastore and NICs. Thick→Thin is a Storage vMotion transform (same datastore is fine). Same cluster only when changing hosts. **50 VMs per job** is a vFleet relay cap (not vMotion); extra VMs are deferred to a second batch. |
| Create VM | Clone from a template already on the remote side | Small SOAP call. Prefer this over uploading an OVA across a WAN. Resumes by reattaching the vCenter task. |
| Datastores | Inventory + datastore browser | Capacity, free space, browse folders, mkdir, delete file. Last listing is cached when vCenter is unreachable. |
| Upload ISO / OVA | Local staging, then resumable push | File lands on this machine first. The relay then uploads via Content Library (byte resume) or datastore PUT with retry. |
| Group by username / corp prefix | Name regex + optional vCenter custom fields | Default: `mzipkin-win11-lab` → owner `mzipkin`. If a VM has a custom field named Owner/User/CreatedBy, that wins. |
| Per-user report | Aggregated in the Owners view | VM count, on/off/suspended, total vCPU, total RAM, idle candidates. CSV export. |
| “Not accessed in a long time” | **Inferred**, not true OS last-login | vCenter does not store Windows/Linux last-login. vFleet uses last console ticket, power events, reconfigure/migrate events (lookback window), and `bootTime`. Idle CPU/RAM on a powered-on VM is the stronger reclaim signal. |
| Reclaim oversized idle labs | Score + confirm UI | Flags powered-on VMs with low utilization, high reservation, and stale activity. You can guest-shutdown, hard power-off, or **delete the VM from disk** (powers off first). Destroy datastore is not exposed. |

The vSphere **REST** Automation API can list VMs and do power operations, but live performance and event history are still best through the SOAP APIs. That is why the backend uses pyvmomi rather than REST-only.

## Standalone ESXi mode

Connect to an ESXi management address exactly as you would connect to vCenter. vFleet detects the endpoint and keeps the existing vCenter adapter/relay architecture intact; vCenter-only controls are hidden rather than emulated.

- Inventory, VM console/power/rename/delete, datastore access, and disk relocation use the vSphere SOAP API directly on the host.
- The **Hosts** view becomes a direct-host console for maintenance mode, guarded reboot/shutdown, ESXi Shell/SSH/NTP service state, NTP configuration, storage rescan, and support-bundle generation.
- VM **Actions → Convert disk provisioning** first builds a disk-by-disk plan. RDM, encrypted, shared, snapshot-dependent SSH, stale-plan, and endpoint-switch hazards are blocked before a job is queued.
- vCenter-only DRS, roles, templates, Content Library, and cross-host migration remain available only when a vCenter endpoint reports those capabilities.
- Some free ESXi licenses expose write APIs as read-only. Use the API method first. The optional SSH fallback is explicitly selected, host-key verified, and limited to `vmkfstools` cloning; it never accepts arbitrary shell commands and preserves source VMDKs.

Every job records the endpoint fingerprint it was created for. A queued task will fail closed rather than run after the operator switches to a different vCenter or ESXi host.

## Honest limits

- **Guest last login / “who is using this desktop”** needs VMware Tools guest operations plus guest credentials (or a guest agent). That is a later, opt-in step because it reaches into the OS.
- **vSphere Tags** are a separate tagging service. v1 reads **custom fields** and the **VM name prefix**. Tags can be added if you use them for owner.
- Historical charts (p95 over 7 days) would use `PerformanceManager`; v1 uses live quickStats plus event recency, which is enough to triage labs.
- Power actions are **dangerous**. The UI requires an explicit confirm. Default bind is `127.0.0.1`. Optional `UI_TOKEN` gates the API.

## Local relay (flaky VPN)

The UI talks only to this machine. A background worker is the only thing that calls vCenter.

- Last-good inventory, datastores, and templates live in `data/vfleet.db`. If the session drops, the console stays up and is marked stale.
- Clones, uploads, mkdir, file delete, and power actions are jobs on disk. On reconnect the worker retries with backoff and reattaches in-flight vCenter tasks.
- Uploads are staged locally first (so a browser refresh cannot lose the file), then pushed. Content Library is preferred because vCenter can resume at a byte offset. If that privilege is missing, the worker falls back to a datastore HTTP PUT and retries.
- Clone-from-template does **not** copy a disk over the VPN. The template must already exist on the remote side.

Set `DATA_DIR` if you want the SQLite file and staging directory somewhere other than `./data`.

## Quick start (demo, no vCenter)

```bash
chmod +x scripts/dev.sh scripts/run.sh
./scripts/dev.sh
```

Open [http://127.0.0.1:5173](http://127.0.0.1:5173). Demo data is shaped like a lab cluster with user-prefixed VM names so grouping and reclaim can be clicked through immediately.

## Manage connections from the UI

The connection card in the left rail opens a searchable drawer of saved vCenter and standalone ESXi endpoints. Add, edit, switch, or remove profiles there; vFleet detects the endpoint type after connecting and exposes only the supported controls.

- **Test** detects and reports the endpoint without saving credentials or switching away from the current session.
- **Connect & save** tests first, stores the profile in an owner-only `data/connections.json`, mirrors the active profile to the gitignored local `.env` for startup compatibility, and switches to live inventory.
- Profile API responses never include passwords. A blank password while editing reuses the retained secret only when the saved endpoint, user, and port still match.
- Switching is refused while queued or running work is bound to the current endpoint.
- **Stay in demo** closes the dialog. **Disconnect** / **Forget saved credentials** are in the rail after you connect.

The Jobs view is scoped to the current endpoint fingerprint. Completed history from other profiles stays hidden until that profile is selected. Queued/retrying jobs can be cancelled; terminal rows can be removed individually or cleared together without deleting active work.

`.env` still works if you prefer to pre-fill values before start.

## Connect your vCenter via `.env`

1. Copy `.env.example` → `.env` (the dev script does this if `.env` is missing).
2. Set:

```
APP_MODE=auto
VCENTER_HOST=vcenter.example.com
VCENTER_USER=readonly-or-operator@vsphere.local
VCENTER_PASSWORD=...
VCENTER_INSECURE=true
```

3. Restart `./scripts/dev.sh`. `APP_MODE=auto` uses vCenter whenever host/user/password are set, otherwise demo.

For a standalone host, use the same variables with its management address and an ESXi account. Optional SSH fallback settings are documented in `.env.example`; leave `ESXI_SSH_ENABLED=false` unless you specifically need it.

## CLI automation

The CLI calls the local FastAPI/relay boundary, so it gets the same confirmations, endpoint binding, retry state, and audit trail as the UI:

```bash
scripts/vfleet connection
scripts/vfleet inventory --kind vms
scripts/vfleet host
scripts/vfleet disk-plan VM_ID --target thin
scripts/vfleet disk-convert VM_ID --target thin --yes
scripts/vfleet jobs
scripts/vfleet job JOB_ID --wait
```

Host lifecycle, service, NTP, storage-rescan, and support-bundle commands are listed by `scripts/vfleet --help`. Mutating commands require an interactive `yes` or `--yes`. Override the local endpoint with `VFLEET_API_URL`; use `VFLEET_UI_TOKEN` when the API is protected.

Keep `.env` off git. Use a dedicated service account with least privilege:

- Read-only on datacenters/clusters/hosts/VMs for inventory
- Virtual machine **Interaction** (power on/off/reset/console) only if you want actions enabled
- Virtual machine **Migrate** privileges if you want Host migrate / Thick→Thin:
  - `Resource.HotMigrate` for powered-on vMotion
  - `Resource.ColdMigrate` for powered-off migrate or disk-type convert
  - `Datastore.Relocate` when changing datastore or converting Thick/Thin
- **Datastores → vMotion / migration access** can assign a dedicated `vFleet-Migrate` role to the current login on a cluster or globally, if that login already has `Authorization.ModifyPermissions` (confirm required). It does not grant Administrator.
- Virtual machine **Inventory** (delete from disk) only if you want reclaim destroy enabled
- Datastore destroy is **not** used

## Production-style single port

```bash
./scripts/run.sh
```

Builds the UI and serves it from FastAPI at `http://127.0.0.1:8081` (8080 is often Homebrew nginx on this machine).

Set `UI_TOKEN` in `.env` and paste the same value into the left-rail **UI token** field if you bind beyond localhost.

## Naming convention

Default pattern: `^([A-Za-z][A-Za-z0-9]+)`

| VM name | Owner |
| --- | --- |
| `mzipkin-win11-lab` | `mzipkin` |
| `jdoe_rhel_lab01` | `jdoe` |
| `achen.ml-gpu02` | `achen` |

Override with `NAME_GROUP_PATTERN` if your corp prefix is different (for example `^corp-([^-]+)-`).

## Reclaim heuristics

A powered-on VM scores higher when:

- CPU usage ≤ `CPU_IDLE_PCT` (default 8%)
- Guest memory usage ≤ `MEMORY_IDLE_PCT` (default 20%)
- No console/power activity for `IDLE_DAYS` (default 14)
- Large reservation (8+ GiB or 4+ vCPU while idle)

Tune those in `.env`. Prefer **Guest shutdown** when Tools is running. **Delete from disk** is irreversible and requires a confirm.

## Layout

```
backend/app          FastAPI + demo/vCenter adapters + local relay
frontend/src         React UI
scripts/vfleet       Local API/relay CLI
scripts/dev.sh       API :8081 + Vite :5173
data/                SQLite job queue, last inventory, staging uploads (gitignored)
.env.example         Credential stub
```

## Tests

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest
```

## License

Copyright (C) 2026 Michael Zipkin

This program is free software: you can redistribute it and/or modify it under the terms of the
**GNU Affero General Public License** as published by the Free Software Foundation, either version 3
of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but **WITHOUT ANY WARRANTY**;
without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
See the [GNU AGPL v3](LICENSE) for details.

Key restriction: if you run a modified version of this software as a network service, you must
make the complete corresponding source code available to users of that service (AGPL §13).
