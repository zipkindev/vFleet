import { useEffect, useRef, useState } from "react";
import {
  createStaging, putStagingChunk, inspectHostUpgrade, fetchHostUpgrade, fetchActiveHostUpgrade,
  fetchUpgradeJob, prepareHostUpgrade, recoverHostUpgrade, queueHostService,
  fetchUpgradeUsbDevices, writeUpgradeUsb,
} from "./api";
import type { HostUpgradePlan, UpgradeUsbDevice, UpgradeUsbInventory } from "./api";
import type { Job } from "./types";

export function HostUpgradePanel() {
  const [plan, setPlan] = useState<HostUpgradePlan | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [checksum, setChecksum] = useState("");
  const [order, setOrder] = useState<string[]>([]);
  const [checks, setChecks] = useState<Record<string, boolean>>({});
  const [confirmAction, setConfirmAction] = useState<"" | "ssh" | "prepare" | "complete" | "abort">("");
  const [usbInventory, setUsbInventory] = useState<UpgradeUsbInventory | null>(null);
  const [usbNumber, setUsbNumber] = useState("");
  const [usbConfirmation, setUsbConfirmation] = useState("");
  const alive = useRef(true);
  const ack = [
    ["path_verified", "I verified the exact source and target builds in Broadcom’s upgrade matrix."],
    ["hardware_verified", "I verified ESXi 8 compatibility for the CPU, NIC/storage drivers, firmware, boot device, and TPM."],
    ["vm_backups_verified", "I have usable off-host VM backups and recovery media for the current ESXi build."],
    ["independent_controller", "vFleet and its network/VPN/jump-host access run independently of this ESXi host."],
    ["installer_ready", "Bootable installer media and local console access are ready. I will select Upgrade, preserving VMFS."],
    ["manage_ssh_service", "Allow vFleet to start SSH when needed and stop it when this upgrade workflow finishes."],
  ];
  const active = plan !== null && (Boolean(plan.reserved) || !["planned", "complete", "aborted"].includes(plan.phase));
  const selectedUsb = usbInventory?.devices.find(device => String(device.number) === usbNumber) || null;
  const usbAllowed = plan !== null && ["planned", "complete", "aborted"].includes(plan.phase);
  const usbProgress = job?.kind === "host_upgrade_usb_write" ? job.progress : null;

  function showPlan(next: HostUpgradePlan) {
    setPlan(next);
    setOrder(next.startup_order || next.context.vms.filter(vm => vm.power_state === "POWERED_ON").map(vm => vm.uuid));
  }

  useEffect(() => {
    alive.current = true;
    void fetchActiveHostUpgrade().then(({ plan: next }) => { if (next && alive.current) showPlan(next); })
      .catch(err => { if (alive.current) setError(String(err.message || err)); });
    return () => { alive.current = false; };
  }, []);

  useEffect(() => {
    if (!job || ["succeeded", "failed", "cancelled"].includes(job.status)) return;
    let stopped = false;
    const timer = window.setInterval(() => {
      void fetchUpgradeJob(job.id).then(async next => {
        if (stopped) return;
        const terminal = ["succeeded", "failed", "cancelled"].includes(next.status);
        const id = next.kind === "host_upgrade_inspect" ? next.id : String(next.payload.plan_id || "");
        if ((next.status === "succeeded" || next.kind !== "host_upgrade_inspect") && id) {
          const updated = await fetchHostUpgrade(id);
          if (stopped) return;
          showPlan(updated);
        }
        if (stopped) return;
        setJob(next);
        if (terminal) {
          setBusy("");
          if (next.error) setError(next.error);
        }
      }).catch(err => { if (!stopped) setError(String(err.message || err)); });
    }, 2000);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [job?.id, job?.status]);

  async function inspect(file: File) {
    setError(""); setBusy("Uploading ISO"); setPlan(null); setChecks({}); setChecksum("");
    try {
      const staging = await createStaging(file.name, file.size);
      const chunk = 4 * 1024 * 1024;
      for (let start = 0; start < file.size; start += chunk) {
        if (!alive.current) return;
        await putStagingChunk(staging.id, file.slice(start, start + chunk), start, file.size);
        setBusy(`Uploading ISO ${Math.min(100, Math.round((start + chunk) / file.size * 100))}%`);
      }
      setJob(await inspectHostUpgrade(staging.id));
      setBusy("Inspecting ISO and live host");
    } catch (err) { setError(err instanceof Error ? err.message : String(err)); setBusy(""); }
  }

  async function startSshAndRecheck() {
    if (!plan) return;
    setError(""); setBusy("Starting ESXi SSH service");
    const waitFor = async (id: string) => {
      for (let attempt = 0; attempt < 120; attempt++) {
        const next = await fetchUpgradeJob(id);
        if (["succeeded", "failed", "cancelled"].includes(next.status)) return next;
        await new Promise(resolve => window.setTimeout(resolve, 500));
      }
      throw new Error("Timed out waiting for the vFleet job");
    };
    try {
      const serviceJob = await queueHostService("TSM-SSH", "start");
      const completed = await waitFor(serviceJob.id);
      if (completed.status !== "succeeded") throw new Error(completed.error || "Could not start the ESXi SSH service");
      setBusy("Rechecking ISO and live host");
      const inspection = await inspectHostUpgrade(plan.staging_id);
      const inspected = await waitFor(inspection.id);
      setJob(inspected);
      if (inspected.status !== "succeeded") throw new Error(inspected.error || "Upgrade preflight failed");
      showPlan(await fetchHostUpgrade(inspection.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("");
    }
  }

  async function prepare() {
    if (!plan) return;
    setError(""); setBusy("Preparing host");
    try {
      setJob(await prepareHostUpgrade({ plan_id: plan.id, confirm: true, publisher_sha256: checksum.trim(),
        startup_order: order, shutdown_timeout_seconds: 600, startup_delay_seconds: 10,
        path_verified: Boolean(checks.path_verified), hardware_verified: Boolean(checks.hardware_verified),
        vm_backups_verified: Boolean(checks.vm_backups_verified), independent_controller: Boolean(checks.independent_controller),
        installer_ready: Boolean(checks.installer_ready),
        manage_ssh_service: Boolean(checks.manage_ssh_service) }));
    } catch (err) { setError(err instanceof Error ? err.message : String(err)); setBusy(""); }
  }

  async function recover(mode: "complete" | "abort") {
    if (!plan) return;
    setError(""); setBusy("Verifying and restoring");
    try { setJob(await recoverHostUpgrade(plan.id, mode)); }
    catch (err) { setError(err instanceof Error ? err.message : String(err)); setBusy(""); }
  }

  async function scanUsbDevices() {
    setError(""); setBusy("Scanning removable USB disks");
    try {
      const inventory = await fetchUpgradeUsbDevices();
      setUsbInventory(inventory);
      setUsbNumber("");
      setUsbConfirmation("");
      if (!inventory.supported) setError(inventory.message);
    } catch (err) { setError(err instanceof Error ? err.message : String(err)); }
    finally { setBusy(""); }
  }

  async function createInstallerUsb(device: UpgradeUsbDevice) {
    if (!plan) return;
    setError(""); setBusy(`Preparing Windows USB writer for Disk ${device.number}`);
    try {
      setJob(await writeUpgradeUsb({
        plan_id: plan.id,
        disk_number: device.number,
        friendly_name: device.friendly_name,
        serial_number: device.serial_number,
        unique_id: device.unique_id,
        size_bytes: device.size_bytes,
        confirmation: usbConfirmation,
      }));
    } catch (err) { setError(err instanceof Error ? err.message : String(err)); setBusy(""); }
  }

  function runConfirmedAction() {
    const action = confirmAction;
    setConfirmAction("");
    if (action === "ssh") void startSshAndRecheck();
    else if (action === "prepare") void prepare();
    else if (action === "complete" || action === "abort") void recover(action);
  }

  const confirmationText = confirmAction === "ssh"
    ? "Start ESXi SSH for the verified backup transport and recheck the host. vFleet will stop SSH immediately after backup, before guest shutdown."
    : confirmAction === "prepare"
      ? "Save and verify the host configuration backup, gracefully shut down the listed guests through the vSphere API, and enter maintenance mode."
      : confirmAction === "complete"
        ? "Verify the upgraded host identity, build, datastores, and registrations; exit maintenance mode; then restore only the saved running VMs."
        : confirmAction === "abort"
          ? "Verify the original ESXi build and restore VMs stopped by this workflow. This cannot undo an installed hypervisor upgrade."
          : "";

  return <div className="panel stack">
    <header><div><h2>ESXi upgrade</h2><p>Automated preparation and recovery with a manual installer boot on servers without remote media control.</p></div></header>
    <p>Select the original ESXi 8.0 U3e free installer ISO. vFleet verifies every boot module declared by the image plus the embedded upgrade metadata and image profile. Mounting or extracting files is unnecessary.</p>
    <div className="banner"><div><strong>Automation available on this server</strong>
      <p>One confirmed preparation queues the host backup, graceful shutdown in reverse order, and maintenance mode. Because this home-built server has no remote media or boot controller, booting the installer remains a physical handoff. After the installer boots ESXi, one recovery action verifies the host and restores the saved VM power states.</p>
      <p>Fully unattended remediation requires an official offline depot ZIP, vCenter Lifecycle Manager, or preconfigured PXE/remote boot control. vFleet does not convert an installer ISO into an unsupported depot.</p>
    </div></div>
    {error && <div className="banner bad">{error}</div>}
    {busy && <div className="banner" role="status">{busy}{job ? ` · ${job.status}` : ""}
      {usbProgress?.message ? <><br /><span>{String(usbProgress.message)}{Number.isFinite(Number(usbProgress.percent)) ? ` · ${Number(usbProgress.percent)}%` : ""}</span></> : null}
    </div>}
    {job?.kind === "host_upgrade_usb_write" && job.status === "succeeded" &&
      <div className="banner good">Installer USB completed and its contents matched the staged ISO SHA-256.</div>}
    {!active && <label>Installer ISO<input type="file" accept=".iso" disabled={Boolean(busy)} onChange={event => {
      const file = event.target.files?.[0]; if (file) void inspect(file);
    }} /></label>}
    {plan && <>
      <p><strong>{plan.context.host.version} build {plan.context.host.build} → {plan.media.version} build {plan.media.build}</strong><br />Workflow: {plan.phase.replace(/_/g, " ")}</p>
      <p className="sub">{plan.notice}</p>
      {!active && <button disabled={Boolean(busy)} onClick={() => {
        setBusy("Rechecking live host"); setError(""); setChecks({});
        void inspectHostUpgrade(plan.staging_id).then(setJob).catch(err => { setError(String(err.message || err)); setBusy(""); });
      }}>Recheck host with this ISO</button>}
      <details><summary>ISO fingerprint and source guidance</summary>
        <p style={{ overflowWrap: "anywhere" }}>SHA-256: {plan.media.sha256}</p>
        <ul>
          <li>Image profile: {plan.media.image_profile || "not reported"}</li>
          <li>Installer payload: {plan.media.installer_complete ? `complete (${plan.media.boot_module_count} declared boot modules verified)` : "not fully verified"}</li>
          <li>Embedded host-upgrade metadata: {plan.media.upgrade_metadata_present ? "present" : "missing"}</li>
          <li>Execution mode: guided physical boot</li>
        </ul>
        <ul>{plan.sources.map(source => <li key={source.url}><a href={source.url} target="_blank" rel="noreferrer">{source.title}</a></li>)}</ul>
      </details>
      {plan.blockers.length > 0 && <div className="banner bad"><div><strong>Preparation blocked</strong><ul>{plan.blockers.map((item, i) => <li key={i}>{item}</li>)}</ul></div></div>}
      {plan.phase === "planned" && plan.blockers.some(item => item.toLowerCase().includes("ssh")) &&
        <button disabled={Boolean(busy)} onClick={() => setConfirmAction("ssh")}>Start SSH &amp; recheck</button>}
      <details open><summary>VM restoration order · {order.length} running VMs</summary>
        <p>Shutdown uses the reverse of this order. Originally powered-off VMs stay off. Startup waits 10 seconds between VMs; application readiness is not inferred.</p>
        <ol>{order.map((uuid, index) => <li key={uuid}>
          {plan.context.vms.find(vm => vm.uuid === uuid)?.name}
          {plan.phase === "planned" && <button disabled={index === 0 || Boolean(busy)} onClick={() => setOrder(current => {
            const next = [...current]; [next[index - 1], next[index]] = [next[index], next[index - 1]]; return next;
          })}>Move earlier</button>}
        </li>)}</ol>
      </details>
      {plan.phase === "planned" && !active && <>
        <label>Publisher SHA-256<input value={checksum} onChange={event => setChecksum(event.target.value)} placeholder="Paste the checksum from the official download source" /></label>
        <div className="upgrade-checklist">
          {ack.map(([key, label]) => <label className="upgrade-check" key={key}><input type="checkbox" checked={Boolean(checks[key])} onChange={event => setChecks(current => ({ ...current, [key]: event.target.checked }))} /><span>{label}</span></label>)}
        </div>
        <button className="danger" disabled={Boolean(busy) || plan.blockers.length > 0 || !ack.every(([key]) => checks[key]) || checksum.trim().toLowerCase() !== plan.media.sha256} onClick={() => setConfirmAction("prepare")}>Back up, shut down guests &amp; prepare host</button>
      </>}
      {active && <>
        <p>{plan.shutdown_completed.length} guests stopped · {plan.restored.length} restored. vFleet reserves this host until recovery finishes.</p>
        {plan.phase === "awaiting_installation" && <div className="banner"><div>
          <strong>Manual installer handoff</strong>
          <ol><li>At the physical console, reboot using the verified bootable ISO/USB.</li>
            <li>Select the existing ESXi boot device and <strong>Upgrade ESXi, preserve VMFS datastore</strong>. Stop if Upgrade is unavailable; do not choose Install.</li>
            <li>Remove installer media, boot the upgraded host, and ensure SSH is reachable using the existing trusted key.</li>
            <li>Return here to verify the build, datastores and VM registrations, then restore the saved power states.</li></ol>
        </div></div>}
        <div className="inline-actions">
          <button disabled={Boolean(busy) || !["awaiting_installation", "restoring"].includes(plan.phase)} onClick={() => setConfirmAction("complete")}>Verify upgrade &amp; restore VMs</button>
          <button disabled={Boolean(busy)} onClick={() => setConfirmAction("abort")}>Abort / restore original host</button>
          <button disabled={Boolean(busy)} onClick={() => void fetchHostUpgrade(plan.id).then(showPlan).catch(err => setError(String(err.message || err)))}>Refresh workflow</button>
        </div>
      </>}
      {usbAllowed && <section className="upgrade-usb stack">
        <div><h3>Create installer USB</h3>
          <p>Available only from the native Windows application on the computer with the attached USB. macOS, Linux, and container builds report this writer as unavailable. Windows writes the verified image directly, erasing the selected disk.</p>
        </div>
        <button disabled={Boolean(busy)} onClick={() => void scanUsbDevices()}>{usbInventory ? "Rescan USB disks" : "Scan USB disks"}</button>
        {usbInventory?.supported && <>
          <label>Removable USB disk
            <select value={usbNumber} disabled={Boolean(busy)} onChange={event => { setUsbNumber(event.target.value); setUsbConfirmation(""); }}>
              <option value="">Select a USB disk</option>
              {usbInventory.devices.map(device => <option key={device.number} value={device.number} disabled={!device.safe}>
                Disk {device.number} · {device.friendly_name} · {(device.size_bytes / 1073741824).toFixed(1)} GiB{device.drive_letters.length ? ` · ${device.drive_letters.join(", ")}:` : ""}{device.safe ? "" : " · blocked"}
              </option>)}
            </select>
          </label>
          {usbInventory.devices.length === 0 && <div className="banner">No USB disks are currently visible to Windows. Insert the USB, then rescan.</div>}
          {selectedUsb && <div className="usb-identity">
            <strong>Disk {selectedUsb.number}: {selectedUsb.friendly_name}</strong>
            <span>{(selectedUsb.size_bytes / 1073741824).toFixed(2)} GiB · {selectedUsb.drive_letters.length ? `Drive ${selectedUsb.drive_letters.join(", ")}:` : "No mounted drive letter"}</span>
            <span>Device ID: {selectedUsb.serial_number || selectedUsb.unique_id}</span>
            {!selectedUsb.safe && <span className="bad-text">{selectedUsb.blocked_reason}</span>}
          </div>}
          {selectedUsb?.safe && <>
            <label>Type <strong>ERASE USB DISK {selectedUsb.number}</strong> to confirm
              <input value={usbConfirmation} onChange={event => setUsbConfirmation(event.target.value)} autoComplete="off" />
            </label>
            <p className="sub">Windows will request administrator access, revalidate this exact device, raw-write the ISO, and read it back for SHA-256 verification. Keep the browser and vFleet running.</p>
            <button className="danger" disabled={Boolean(busy) || usbConfirmation !== `ERASE USB DISK ${selectedUsb.number}`}
              onClick={() => void createInstallerUsb(selectedUsb)}>Erase selected USB &amp; create installer</button>
          </>}
        </>}
      </section>}
      {["complete", "aborted"].includes(plan.phase) && <div className="banner good">Host identity, maintenance state, SSH closeout, and saved VM power states were verified. Check guest applications before closing the workflow.</div>}
    </>}
    {confirmAction && <div className="upgrade-confirm" role="alertdialog" aria-modal="true" aria-labelledby="upgrade-confirm-title">
      <strong id="upgrade-confirm-title">Confirm host workflow action</strong>
      <p>{confirmationText}</p>
      <div className="inline-actions">
        <button onClick={() => setConfirmAction("")}>Cancel</button>
        <button className={confirmAction === "prepare" ? "danger" : "accent"} onClick={runConfirmedAction}>Confirm and continue</button>
      </div>
    </div>}
  </div>;
}
