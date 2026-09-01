# vFleet

A local web console for **your** vCenter or standalone ESXi host: inventory clusters/hosts/VMs, group labs by naming convention, report resource use by owner, and run guarded operator workflows.

This talks only to endpoints you configure. UI-saved credentials live in vFleet's portable encrypted JSON vault; `.env` is retained as an explicit bootstrap option. It is an operator tool, not a scanner for systems you do not administer.

## What is possible

| Need | How | Notes |
| --- | --- | --- |
| Authenticate | vSphere session via [pyvmomi](https://github.com/vmware/pyvmomi) (SOAP) | Auto-detects vCenter (`VirtualCenter`) or direct ESXi (`HostAgent`). |
| Clusters, hosts, VMs | PropertyCollector inventory | Names, power state, guest OS, Tools, IP, configured vCPU/RAM, current host/cluster. Cached locally so the UI keeps working if the VPN drops. |
| CPU / memory utilization | `summary.quickStats` | Live usage (MHz / guest memory). This is the same family of numbers the vSphere UI uses, not guest-inside telemetry. |
| Power on / off / reset / suspend | Local job queue → VIM power methods | **Guest shutdown/reboot** needs VMware Tools. Hard power-off does not. Queued on disk; retried after disconnects. |
| Install VMware Tools | Windows WinRM + host Tools ISO; Linux SSH + `open-vm-tools`; pfSense SSH + `pfSense-pkg-Open-VM-Tools` | Single or bulk action. Uses encrypted Automation Vault credentials, pins target and optional jump-host SSH keys, and gives every guest an independent endpoint-bound job. |
| Configure VM hardware | Reviewed local job → power workflow → `ReconfigVM_Task` | Single or bulk shared desired state for vCPU, RAM, one disk target size, and datastore ISO mount/eject. Powered-on VMs can use an explicit guest-shutdown workflow, an optional forced-power-off fallback, and restoration of VMs that were originally running. Disk shrink is never allowed. |
| Migrate host / convert disks | Local job → vCenter `RelocateVM` or verified ESXi SSH | vCenter uses Storage vMotion. Direct ESXi uses an allowlisted `vmkfstools` clone because HostAgent can reject in-place relocation. **50 VMs per job** is a vFleet relay cap; extra VMs are deferred to a second batch. |
| Reconcile storage | Live inventory + conversion history + guarded datastore scan | Single VM, bulk selection, or full datastore scan. Finds preserved conversion sources, unattached VMDKs, and unregistered VM folders without deleting anything until a fresh plan and explicit confirmation. |
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

- Inventory, VM console/power/rename/delete, datastore access, and host administration use the vSphere SOAP API directly on the host.
- The **Hosts** view becomes a direct-host console for maintenance mode, guarded reboot/shutdown, ESXi Shell/SSH/NTP service state, NTP configuration, storage rescan, and support-bundle generation.
- VM **Actions → Convert disk provisioning** first builds a disk-by-disk plan. RDM, encrypted, shared, snapshot-dependent SSH, stale-plan, and endpoint-switch hazards are blocked before a job is queued.
- **Machines → Configure hardware** applies one reviewed configuration to up to 50 VMs. It can automatically expand the target scope to every VM in the selected cluster(s), while retaining per-VM blockers and results. Disk selection is ordinal (`Disk 1`, `Disk 2`, …) so different vSphere device keys do not break a bulk change. The optional power workflow requests guest shutdown through VMware Tools, forces power off after two minutes only when explicitly enabled, and powers back on only VMs that were running when execution began.
- vCenter-only DRS, roles, templates, Content Library, and cross-host migration remain available only when a vCenter endpoint reports those capabilities.
- Standalone HostAgent can advertise relocation capabilities but reject an in-place disk format change with `NotSupported`, even on a licensed host. Automatic disk conversion therefore uses verified SSH on direct ESXi. vFleet temporarily starts the SSH service for the job, restores its prior stopped state afterward, limits execution to `vmkfstools` cloning, and preserves source VMDKs. The API relocation path remains an explicitly labeled advanced option.
- Disk conversions reconcile storage by default. A preserved source is cleanup-eligible only when the replacement is attached and a post-conversion boot is validated with VMware Tools. If Tools is absent, the UI offers the existing automated Tools deployment or a manual post-boot validation acknowledgement. Powered-off VMs remain blocked.

Every job records the endpoint fingerprint it was created for. A queued task will fail closed rather than run after the operator switches to a different vCenter or ESXi host.

## Guest automation and the Automation Vault

The **Vault** page stores reusable Windows/WinRM, Linux/SSH, and general service credentials. Non-secret metadata is portable JSON; each password or token is a separate AES-256-GCM record protected by the same external master-key strategy used for vSphere connections. API responses and job payloads contain credential IDs only.

Select one or more powered-on VMs and choose **Deploy VMware Tools**:

- Windows Server and desktop guests use WinRM with encrypted NTLM messages (or HTTPS), verify local-administrator access, mount the ESXi/vCenter-provided Tools ISO, run the silent installer, verify Tools after the scheduled reboot, and restore prior CD media.
- Linux guests use password-authenticated SSH with a reviewed SHA-256 host-key fingerprint. The relay installs the distribution-supported `open-vm-tools` package through apt, dnf/yum, zypper, tdnf, or apk and validates `vmtoolsd`.
- pfSense guests use a root SSH credential and the signed `pfSense-pkg-Open-VM-Tools` package from the firewall's configured pfSense repository. Preflight fails closed unless the target identifies itself as pfSense, and the job verifies `vmtoolsd` without requesting a reboot.
- Saved vCenter and standalone ESXi profiles can define a default one-hop SSH access path using a separately vaulted credential and pinned jump-host key. The jump-host type can be auto-discovered or set to Windows OpenSSH/PowerShell or Linux/Unix. VMware Tools deployments inherit it automatically, while operators can disable or override it for one deployment.
- SSH guests can optionally connect through that inherited or one-off jump host. Both host keys are reviewed and pinned, and neither password enters the job payload. Standalone ESXi's allowlisted SSH disk-conversion fallback uses the saved access path too; vCenter HTTPS/SOAP remains direct.
- Mixed and bulk selections remain independent jobs. One guest failure does not roll back or fail other guests.
- **Run a remote dry run before queueing** is enabled by default. It authenticates, verifies platform/root access and pinned keys, and checks package state without installing; operators can explicitly skip it for restricted environments, and the review banner records that choice.
- The Machines memory cell distinguishes guest-active memory from ESXi host-consumed memory and displays balloon, swap, and compression counters when non-zero; the VM identity line also shows whether VMware Tools is running.

The guest must be directly reachable from the vFleet host or reachable through the configured SSH jump host. Windows requires WinRM and an administrator credential; Linux requires SSH and root or sudo; pfSense requires root SSH. VMware Guest Operations cannot bootstrap a first Tools installation because that API itself depends on a running Tools agent.

## Storage reconciliation

Use **Machines → Reconcile storage** for one or more selected VMs, or **Datastores → Reconcile inventory** for an endpoint-wide review. The scan compares live disk attachments with successful conversion results and first-level datastore folders.

- Preserved source disks from vFleet conversions are high-confidence candidates only when the replacement disk remains attached.
- VMware Tools plus a recorded post-conversion boot provides automated validation. Without Tools, power on the VM and manually validate guest operation before acknowledging cleanup.
- Unattached VMDKs and directories containing VM files but no current attachment are review-only candidates. They may represent an intentionally unregistered VM, backup, or recovery copy.
- Cleanup rebuilds the plan server-side, refuses stale inventory or changed datastore state, and queues each explicitly confirmed item as an independent endpoint-bound delete job.

## Honest limits

- **Guest last login / “who is using this desktop”** is not collected. The Automation Vault and Tools deployment are explicit opt-in guest access; vFleet does not scan guest accounts or retain command output containing secrets.
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
- **Connect & save** tests first, stores non-secret profile metadata in `data/connections.json`, encrypts passwords in `data/credentials.enc.json`, and switches to live inventory.
- Profile API responses never include passwords. A blank password while editing reuses the retained secret only when the saved endpoint, user, and port still match.
- **Network access** on Add/Edit connection assigns an optional jump-host address, host type, Automation Vault credential, and pinned host key to that profile. **Auto detect** probes the authenticated SSH shell without changing the host; an explicit Windows OpenSSH/PowerShell or Linux/Unix choice is available for restricted shells. **Test jump host & pin key** verifies authentication and host identity before the association can be saved.
- A referenced jump credential cannot be deleted until it is removed from the connection profile. Connection profiles store only its credential ID; the secret stays in the encrypted Automation Vault.
- Switching is refused while queued or running work is bound to the current endpoint.
- **Stay in demo** closes the dialog. **Disconnect** / **Forget saved credentials** are in the rail after you connect.

The Jobs view is scoped to the current endpoint fingerprint. Completed history from other profiles stays hidden until that profile is selected. Queued/retrying jobs can be cancelled; terminal rows can be removed individually or cleared together without deleting active work.

The credential vault uses AES-256-GCM with a unique nonce and profile-bound authenticated data for every saved connection. Its randomly generated master key defaults to `~/.vfleet/credential.key`, outside the repository and `DATA_DIR`. Override that portable path with `VFLEET_MASTER_KEY_FILE`; service deployments should mount the key separately. `VFLEET_MASTER_KEY` can inject a base64-encoded 32-byte key directly into the process environment.

Legacy plaintext profiles and `.env` passwords are migrated automatically after the encrypted records are written successfully. `.env` remains an explicit bootstrap/development input, but UI-created connections never write secrets into it.

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

3. Restart `./scripts/dev.sh`. On the first successful start, vFleet imports the connection into the encrypted vault and clears the plaintext password fields from `.env`.

For a standalone host, use the same variables with its management address and an ESXi account. Optional SSH fallback settings are documented in `.env.example`; leave `ESXI_SSH_ENABLED=false` unless you specifically need it.

## CLI automation

The CLI calls the local FastAPI/relay boundary, so it gets the same confirmations, endpoint binding, retry state, and audit trail as the UI:

```bash
scripts/vfleet connection
scripts/vfleet inventory --kind vms
scripts/vfleet host
scripts/vfleet disk-plan VM_ID --target thin
scripts/vfleet disk-convert VM_ID --target thin --yes
scripts/vfleet hardware-plan VM_ID_A VM_ID_B --cpu 4 --memory-gib 8
scripts/vfleet hardware-config VM_ID_A VM_ID_B --disk 1 --disk-gib 120 --iso-datastore DATASTORE_ID --iso-path isos/rhel9.iso --yes
scripts/vfleet hardware-config VM_ID_A VM_ID_B --cpu 2 --shutdown-before --force-power-off --power-on-after --yes
scripts/vfleet credentials
scripts/vfleet credential-add "Linux admins" --kind ssh --user operator
scripts/vfleet connect vcenter.example.com --user administrator@vsphere.local --jump-address access.example.com --jump-host-type auto --jump-credential-id JUMP_CREDENTIAL_ID
scripts/vfleet tools-deploy CREDENTIAL_ID VM_ID=10.20.1.8 --yes
scripts/vfleet tools-deploy PFSENSE_CREDENTIAL_ID VM_ID=10.0.0.1 --os pfsense --jump-address 192.168.1.60 --jump-host-type windows --jump-credential-id JUMP_CREDENTIAL_ID --yes
scripts/vfleet jobs
scripts/vfleet job JOB_ID --wait
```

`tools-deploy` inherits the active connection profile's jump host by default. Pass `--no-jump` to use direct guest SSH for one invocation, or pass explicit `--jump-*` options to override the profile.

Host lifecycle, service, NTP, storage-rescan, and support-bundle commands are listed by `scripts/vfleet --help`. Mutating commands require an interactive `yes` or `--yes`. Override the local endpoint with `VFLEET_API_URL`; use `VFLEET_UI_TOKEN` when the API is protected.

Keep `.env`, the encrypted vault, and especially the separate master key off git. Back up the key separately: losing it makes saved credentials unrecoverable. Use a dedicated service account with least privilege:

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
