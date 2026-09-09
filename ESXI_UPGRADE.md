# ESXi host upgrade workflow

Status: implemented and validated through a complete lab upgrade on 2026-09-09.

The lab host was upgraded from ESXi 7.0.3 build 23307199 to ESXi 8.0.3 build
24677879 with `VMware-VMvisor-Installer-8.0U3e-24677879.x86_64.iso`. vFleet
prepared the host, the operator booted the physical installer USB, and vFleet
verified and restored the host. Six originally running VMs returned to running,
three originally stopped VMs stayed stopped, maintenance mode was cleared, and
SSH was stopped.

## Product placement and operating model

The workflow lives in **Hosts -> ESXi upgrade**. Jobs contains durable inspection,
USB creation, preparation, and recovery jobs. Reloading the page reloads the latest
endpoint-bound plan from SQLite.

A host with remote virtual media and boot control can automate more of the physical
handoff. This home-built lab server has neither, so booting the installer and choosing
the existing ESXi boot device remain local-console steps. vFleet never labels this
physical installer interaction as unattended.

The workflow uses the following transport rules:

| Work | Primary transport | Reason |
| --- | --- | --- |
| Host version, build, UUID, services, datastores, VM registrations and power | vSphere SOAP | Fast, structured, and already authenticated |
| Graceful guest shutdown | vSphere SOAP guest operation | VMware Tools performs the guest shutdown; no guest IP or guest SSH is required |
| Enter/exit maintenance mode | vSphere SOAP task | Task state is observable and reattachable |
| Restore saved VM power states | vSphere SOAP tasks | Stable UUID and VMX matching survives changed numeric VM IDs |
| Host configuration backup | Verified ESXi SSH | The firmware backup and archive transfer are not exposed by the required SOAP path |
| USB creation on a WSL-hosted controller | Windows PowerShell | Windows owns the physical USB disk |

SSH is enabled through SOAP before the first SSH connection when the operator
authorizes service management. The backup sequence reuses one verified SSH session.
As soon as the local backup hash is persisted, vFleet stops SSH through SOAP before
guest shutdown begins. Recovery verifies final SSH closeout again. A reconnect checks
the SOAP service state before attempting SSH; a missing service is not handled by blind
connection retries.

## Installer media inspection

The operator selects the original ISO. vFleet uploads it to local staging and parses
ISO9660 metadata without mounting or executing content. It validates `boot.cfg`, every
declared boot module, the installer/update engine, image payload archives, and the
`UPGRADE` metadata/profile. It displays the embedded target version/build, profile,
size, and complete-file SHA-256. The operator must still compare that SHA-256 with the
publisher source; a filename and a locally computed digest do not authenticate a file.

Validated lab artifact:

- File: `VMware-VMvisor-Installer-8.0U3e-24677879.x86_64.iso`
- Size: 648374272 bytes
- Embedded target: ESXi 8.0.3 build 24677879
- Profile: `ESXi-8.0U3e-24677879-standard`
- SHA-256: `9782c96ffd01cc56da17ec31573da69f4cba2f9402e67c8b55d05d9472c7376a`

An installer ISO is not an offline depot ZIP. vFleet does not extract its VIBs and
pretend they are a supported ESXCLI depot. ESXCLI profile updates require an official
offline depot and its declared image profile. Lifecycle Manager is a separate vCenter
workflow. See Broadcom's [upgrade methods](https://knowledge.broadcom.com/external/article/390293),
[profile update procedure](https://knowledge.broadcom.com/external/article/343840), and
[offline bundle guidance](https://knowledge.broadcom.com/external/article/343425).

## Installer USB creation

After successful ISO inspection, the panel can scan USB disks visible to Windows.
It shows the Windows disk number, model, capacity, drive letters, and stable device
identity. Boot, system, offline, read-only, and unidentified disks are blocked.

The operator must type `ERASE USB DISK N` for the selected disk. Immediately before
writing, the elevated PowerShell helper repeats every identity and safety check and
rechecks the staged ISO size and SHA-256. It dismounts volumes only on that disk, writes
the ISO byte-for-byte to `\\.\PhysicalDriveN`, flushes it, reads back exactly the ISO
length, and verifies the same SHA-256. The image write itself prepares the USB; running
a separate format first is unnecessary. This is the same image-write method used for
the lab USB, which successfully booted this exact ISO on the target server.

The job persists `launched` before elevation. If vFleet restarts after launch and cannot
prove the writer state, it fails closed and never launches a second destructive write.
Selecting a disk and typing the confirmation are required again for a new attempt.

## Ordered workflow

1. Inspect the selected ISO and current host. Record exact source/target builds, host
   UUID, datastores, VM UUID/VMX identities, original power states, Tools status,
   maintenance state, management type, vSAN state, autostart, and SSH state.
2. Resolve all blockers. Confirm the Broadcom upgrade matrix, target hardware/drivers,
   off-host VM backups, original-build recovery media, local-console access, independent
   vFleet connectivity, publisher checksum, and bootable installer media.
3. Create the USB from the inspected ISO when needed. Do this before host preparation;
   USB creation is blocked while a host upgrade is active.
4. Review VM startup order. Shutdown is the reverse. Only VMs recorded as running are
   shutdown and later restored; originally stopped VMs stay stopped.
5. Confirm preparation in the visible in-page confirmation panel. The relay reserves
   the endpoint and rejects unrelated mutations until recovery completes.
6. Revalidate the 15-minute plan and the complete staged ISO. Through SOAP, enable SSH
   only when needed. Through one verified SSH session, synchronize host configuration,
   request the backup, transfer it off-host, and hash it. Persist the hash, then stop SSH
   through SOAP.
7. Persist each guest-shutdown intent before calling the SOAP guest shutdown operation.
   Wait up to the configured timeout and stop on timeout. Never force power off.
8. After SOAP confirms every VM is off, enter maintenance mode through a SOAP task and
   verify it from a fresh host context.
9. At the physical console, boot the verified USB. Select the existing ESXi boot device
   and **Upgrade ESXi, preserve VMFS datastore**. If Upgrade is unavailable, stop. Do not
   select a fresh Install option or overwrite a datastore. Remove the USB after the
   installer completes and boot the upgraded host.
10. Choose **Verify upgrade & restore VMs**. vFleet verifies the original hardware UUID,
    expected target build, accessible datastores, stable VM UUID/VMX registrations, and
    saved inventory. It exits maintenance mode and starts only the VMs it stopped, in the
    reviewed order with the configured delay.
11. vFleet verifies every final saved power state and SSH closeout before releasing the
    endpoint reservation. The operator then verifies guest application health; VM power
    state alone does not prove application readiness.

## Durability and failure behavior

- Every remote mutation runs in `RelayWorker`; request handlers only validate and queue.
- Plan, reservation, backup hash, shutdown intent, SOAP submission markers, maintenance
  intent, startup intent, and completion are persisted around external calls.
- A lost guest-shutdown or startup reply is never followed by a blind duplicate. vFleet
  reads current SOAP state and stops on ambiguity.
- A graceful shutdown timeout never escalates to forced power-off.
- Recovery stops on build/UUID mismatch, missing datastore, changed VM identity/path,
  ambiguous power result, or final state mismatch. The reservation stays in place for
  investigation.
- **Abort / restore original host** restores saved VM power states only after verifying
  the original build. It does not downgrade or undo an installed hypervisor upgrade.
- USB writes require an exact disk identity and typed erase phrase at validation and
  execution. A launched write is not automatically retried.
- The physical installer itself cannot be cancelled or rolled back by vFleet. Keep the
  original installer and configuration backup. Broadcom documents additional
  [rollback caveats](https://knowledge.broadcom.com/external/article/386377).

## Version and hardware gates

The plan uses the live source build and ISO metadata; it never assumes a version from a
filename. The example 7.0 U3p to 8.0 U3e path does not imply every 7.0 U3 patch can move
to U3e. Check the current [Broadcom upgrade matrix](https://interopmatrix.broadcom.com/Upgrade?productId=1).
For example, Broadcom states 7.0 U3w requires 8.0 U3g or later:
[KB 418131](https://knowledge.broadcom.com/external/article/418131).

Review CPU, NIC/storage device support, firmware, boot layout, TPM/Secure Boot, OEM
customizations, and VIBs against the current
[compatibility guide](https://compatibilityguide.broadcom.com/). A successful API or
SSH connection does not prove ESXi 8 hardware support. vFleet does not remove drivers,
use `--force`, bypass signatures, or suppress installer warnings.

Broadcom's free ESXi 8.0 U3e license has vCenter/API constraints documented in
[KB 399823](https://knowledge.broadcom.com/external/article/399823). The workflow must
verify the management path available after installation and stop for manual recovery if
the required SOAP inventory becomes unavailable.

## Validation record

- Focused upgrade, SSH lifecycle, and USB safety tests cover media drift, endpoint
  binding, stale plans, confirmations, retries, shutdown timeout/lost replies, backup
  failure, changed identities/datastores, restoration order, abort, reservation recovery,
  USB identity changes, blocked disks, and uncertain writer restart.
- The PowerShell writer is syntax-parsed without performing disk operations.
- Frontend TypeScript and production builds validate the API contract and UI.
- The full demo/unit suite runs against isolated temporary stores. It does not replace
  live ESXi review.
- Live lab recovery completed on ESXi 8.0.3 build 24677879 with all nine VM power states,
  maintenance mode, host identity, and SSH closeout verified.
