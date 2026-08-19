# vFleet

A local web console for **your** vCenter: inventory clusters/hosts/VMs, group labs by naming convention, report resource use by owner, and shut down idle machines that are sitting on CPU and memory.

This talks to vCenter with credentials **you** put in `.env`. It is an operator tool, not a scanner for systems you do not administer.

## What is possible

| Need | How | Notes |
| --- | --- | --- |
| Authenticate | vCenter session via [pyvmomi](https://github.com/vmware/pyvmomi) (SOAP) | Same account you would use in the vSphere Client. A role with inventory + VM power + events is enough; full Administrator is not required. |
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

## Connect from the UI

The left rail has **Connect to vCenter**. Enter host (or `https://vcenter.example.com:443`), username, and password.

- **Test** talks to vCenter and reports whether the account works, without leaving demo.
- **Save** tests first, then writes host and credentials to local `.env` and switches to live inventory.
- **Stay in demo** closes the dialog. **Disconnect** / **Forget saved credentials** are in the rail after you connect.

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
