import { useEffect, useMemo, useState } from "react";

import { configureVmHardware, fetchInventory, planVmHardware } from "./api";
import { BatchCount } from "./BatchCount";
import { BATCH_LIMIT } from "./batch";
import { bytes, gib } from "./format";
import type { Catalog, Job, VirtualMachine, VmHardwarePlan, VmHardwareSpec } from "./types";

const GIB = 1024 ** 3;

type Props = {
  vms: VirtualMachine[];
  allVms: VirtualMachine[];
  catalog: Catalog | null;
  onClose: () => void;
  onQueued: (job: Job, queuedVmIds: string[], skipped: number) => void;
};

export function VmHardwareModal({ vms, allVms, catalog, onClose, onQueued }: Props) {
  const [includeClusterPeers, setIncludeClusterPeers] = useState(false);
  const [clusterInventory, setClusterInventory] = useState(allVms);
  const [cpuEnabled, setCpuEnabled] = useState(false);
  const [cpuCount, setCpuCount] = useState(String(vms[0]?.cpu_count || 2));
  const [memoryEnabled, setMemoryEnabled] = useState(false);
  const [memoryGib, setMemoryGib] = useState(String((vms[0]?.memory_mib || 2048) / 1024));
  const [diskEnabled, setDiskEnabled] = useState(false);
  const [diskNumber, setDiskNumber] = useState("1");
  const [diskGib, setDiskGib] = useState(
    String(Math.max(1, Math.ceil(Math.max(...vms.map((vm) => vm.disks[0]?.capacity_bytes || GIB)) / GIB))),
  );
  const [isoAction, setIsoAction] = useState<"keep" | "mount" | "eject">("keep");
  const [isoDatastoreId, setIsoDatastoreId] = useState("");
  const [isoPath, setIsoPath] = useState("");
  const [isoAtBoot, setIsoAtBoot] = useState(true);
  const [shutdownBefore, setShutdownBefore] = useState(false);
  const [forcePowerOff, setForcePowerOff] = useState(false);
  const [powerOnAfter, setPowerOnAfter] = useState(false);
  const [plan, setPlan] = useState<VmHardwarePlan | null>(null);
  const [reviewedSpec, setReviewedSpec] = useState<VmHardwareSpec | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const selectedClusterIds = useMemo(
    () => new Set(vms.map((vm) => vm.cluster_id).filter(Boolean)),
    [vms],
  );
  const clusterPeers = useMemo(
    () => clusterInventory.filter((vm) => selectedClusterIds.has(vm.cluster_id)),
    [clusterInventory, selectedClusterIds],
  );
  const targets = includeClusterPeers ? clusterPeers : vms;
  const clusterSummary = useMemo(() => {
    const counts = new Map<string, number>();
    for (const vm of targets) {
      const name = vm.cluster_name || vm.host_name || "Standalone";
      counts.set(name, (counts.get(name) || 0) + 1);
    }
    return [...counts.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [targets]);
  const canExpandClusters = clusterPeers.length > vms.length && selectedClusterIds.size > 0;

  useEffect(() => {
    let current = true;
    void fetchInventory().then((inventory) => {
      if (current) setClusterInventory(inventory.vms);
    }).catch(() => {
      /* Keep the already loaded view if a fresh full inventory read is unavailable. */
    });
    return () => { current = false; };
  }, []);

  useEffect(() => {
    setPlan(null);
    setReviewedSpec(null);
  }, [includeClusterPeers, cpuEnabled, cpuCount, memoryEnabled, memoryGib, diskEnabled, diskNumber, diskGib, isoAction, isoDatastoreId, isoPath, isoAtBoot, shutdownBefore, forcePowerOff, powerOnAfter]);

  function buildSpec(): VmHardwareSpec {
    if (targets.length === 0) throw new Error("Select at least one VM");
    if (targets.length > BATCH_LIMIT) {
      throw new Error(`Cluster expansion selected ${targets.length} VMs; one reviewed job is limited to ${BATCH_LIMIT}`);
    }
    const spec: VmHardwareSpec = {
      vm_ids: targets.map((vm) => vm.id),
      iso_action: isoAction,
      shutdown_before: shutdownBefore,
      force_power_off_on_timeout: shutdownBefore && forcePowerOff,
      power_on_after: shutdownBefore && powerOnAfter,
      shutdown_timeout_seconds: 120,
    };
    if (cpuEnabled) {
      const value = Number(cpuCount);
      if (!Number.isInteger(value) || value < 1 || value > 128) throw new Error("vCPU must be a whole number from 1 to 128");
      spec.cpu_count = value;
    }
    if (memoryEnabled) {
      const value = Number(memoryGib);
      if (!Number.isFinite(value) || value < 0.125 || value > 1024) throw new Error("Memory must be between 0.125 and 1024 GiB");
      spec.memory_mib = Math.round(value * 1024);
    }
    if (diskEnabled) {
      const ordinal = Number(diskNumber);
      const capacity = Number(diskGib);
      if (!Number.isInteger(ordinal) || ordinal < 1 || ordinal > 64) throw new Error("Disk number must be from 1 to 64");
      if (!Number.isFinite(capacity) || capacity < 1) throw new Error("Target disk size must be at least 1 GiB");
      spec.disk_index = ordinal - 1;
      spec.disk_capacity_bytes = Math.round(capacity * GIB);
    }
    if (isoAction === "mount") {
      if (!isoDatastoreId || !isoPath.trim()) throw new Error("Choose a datastore and enter the ISO path");
      spec.iso_datastore_id = isoDatastoreId;
      spec.iso_path = isoPath.trim();
      spec.iso_connect_at_power_on = isoAtBoot;
    }
    if (!cpuEnabled && !memoryEnabled && !diskEnabled && isoAction === "keep") {
      throw new Error("Choose at least one CPU, memory, disk, or ISO change");
    }
    return spec;
  }

  async function review() {
    setBusy(true);
    setError("");
    try {
      const spec = buildSpec();
      const next = await planVmHardware(spec);
      setReviewedSpec(spec);
      setPlan(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not plan hardware changes");
    } finally {
      setBusy(false);
    }
  }

  async function queue() {
    if (!plan || !reviewedSpec || plan.can_execute_count < 1) return;
    setBusy(true);
    setError("");
    try {
      const job = await configureVmHardware(reviewedSpec, plan.plan_token);
      onQueued(
        job,
        plan.targets.filter((target) => target.can_execute).map((target) => target.vm_id),
        plan.blocked_count + plan.noop_count,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not queue hardware changes");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-back" onClick={() => !busy && onClose()}>
      <div className="modal hardware-modal" onClick={(event) => event.stopPropagation()}>
        <h2>Configure VM hardware</h2>
        <p>
          One desired configuration is applied to every eligible VM. Review catches power-state, hot-add, disk, and ISO
          compatibility before the persistent bulk job is queued.
        </p>

        <div className="hardware-scope">
          <span><BatchCount count={targets.length} /> targeted</span>
          <span>{clusterSummary.map(([name, count]) => `${name} (${count})`).join(" · ")}</span>
        </div>
        {canExpandClusters ? (
          <label className="check hardware-cluster-toggle">
            <input
              type="checkbox"
              checked={includeClusterPeers}
              onChange={(event) => setIncludeClusterPeers(event.target.checked)}
            />
            Include every VM in the selected cluster{selectedClusterIds.size === 1 ? "" : "s"} ({clusterPeers.length})
          </label>
        ) : null}
        {targets.length > BATCH_LIMIT ? (
          <div className="banner bad">This cluster scope exceeds the {BATCH_LIMIT}-VM reviewed batch limit.</div>
        ) : null}

        <div className="hardware-grid">
          <fieldset className="choice">
            <legend>Compute</legend>
            <label className="check">
              <input type="checkbox" checked={cpuEnabled} onChange={(event) => setCpuEnabled(event.target.checked)} />
              Set the same vCPU count
            </label>
            <input type="number" min="1" max="128" step="1" disabled={!cpuEnabled} value={cpuCount} onChange={(event) => setCpuCount(event.target.value)} />
            <label className="check">
              <input type="checkbox" checked={memoryEnabled} onChange={(event) => setMemoryEnabled(event.target.checked)} />
              Set the same memory (GiB)
            </label>
            <input type="number" min="0.125" max="1024" step="0.125" disabled={!memoryEnabled} value={memoryGib} onChange={(event) => setMemoryGib(event.target.value)} />
          </fieldset>

          <fieldset className="choice">
            <legend>Storage</legend>
            <label className="check">
              <input type="checkbox" checked={diskEnabled} onChange={(event) => setDiskEnabled(event.target.checked)} />
              Expand the same disk position
            </label>
            <div className="hardware-inline">
              <label>Disk #<input type="number" min="1" max="64" disabled={!diskEnabled} value={diskNumber} onChange={(event) => setDiskNumber(event.target.value)} /></label>
              <label>Target GiB<input type="number" min="1" step="1" disabled={!diskEnabled} value={diskGib} onChange={(event) => setDiskGib(event.target.value)} /></label>
            </div>
            <small>Disk 1 means the first virtual disk on each VM. Disks are expanded to a target size, never by an unbounded increment.</small>
          </fieldset>

          <fieldset className="choice hardware-iso">
            <legend>CD/DVD ISO</legend>
            <label>Action
              <select value={isoAction} onChange={(event) => setIsoAction(event.target.value as typeof isoAction)}>
                <option value="keep">Keep current media</option>
                <option value="mount">Mount the same datastore ISO</option>
                <option value="eject">Disconnect mounted ISO</option>
              </select>
            </label>
            {isoAction === "mount" ? (
              <>
                <label>Datastore
                  <select value={isoDatastoreId} onChange={(event) => setIsoDatastoreId(event.target.value)}>
                    <option value="">Choose datastore…</option>
                    {(catalog?.datastores ?? []).filter((item) => item.accessible).map((item) => (
                      <option key={item.id} value={item.id}>{item.name} · {bytes(item.free_bytes)} free</option>
                    ))}
                  </select>
                </label>
                <label>ISO path<input value={isoPath} onChange={(event) => setIsoPath(event.target.value)} placeholder="isos/windows11.iso" /></label>
                <label className="check"><input type="checkbox" checked={isoAtBoot} onChange={(event) => setIsoAtBoot(event.target.checked)} />Connect at power on</label>
              </>
            ) : null}
          </fieldset>

          <fieldset className="choice hardware-power-workflow">
            <legend>Power workflow</legend>
            <label className="check">
              <input
                type="checkbox"
                checked={shutdownBefore}
                onChange={(event) => setShutdownBefore(event.target.checked)}
              />
              Shut down powered-on guests before applying
            </label>
            <small>Requests a graceful guest shutdown through VMware Tools and waits up to two minutes.</small>
            <label className="check hardware-dependent-option">
              <input
                type="checkbox"
                disabled={!shutdownBefore}
                checked={forcePowerOff}
                onChange={(event) => setForcePowerOff(event.target.checked)}
              />
              Force power off if guest shutdown stalls
            </label>
            <small>Explicit fallback for guests without working VMware Tools. This can cause guest data loss.</small>
            <label className="check hardware-dependent-option">
              <input
                type="checkbox"
                disabled={!shutdownBefore}
                checked={powerOnAfter}
                onChange={(event) => setPowerOnAfter(event.target.checked)}
              />
              Power back on when the workflow ends
            </label>
            <small>Only restores VMs that were powered on when their job began, including after a failed hardware change.</small>
          </fieldset>
        </div>

        {error ? <div className="banner bad">{error}</div> : null}
        {plan ? (
          <div className="hardware-review">
            <h3>Reviewed result</h3>
            <p>{plan.can_execute_count} eligible · {plan.blocked_count} blocked · {plan.noop_count} already matching</p>
            <div className="hardware-target-list">
              {plan.targets.map((target) => (
                <article key={target.vm_id} className={`hardware-target ${target.blockers.length ? "blocked" : target.noop ? "noop" : "ready"}`}>
                  <header><strong>{target.name}</strong><span>{target.cluster_name || target.host_name || "Standalone"} · {target.power_state}</span></header>
                  {target.changes.length ? <ul>{target.changes.map((change) => <li key={change}>{change}</li>)}</ul> : null}
                  {target.will_shutdown_before ? (
                    <div className="hardware-sequence">
                      Workflow: guest shutdown{target.may_force_power_off ? " (forced power-off fallback enabled)" : ""}
                      {target.will_power_on_after ? " → hardware change → power on" : " → hardware change"}
                    </div>
                  ) : null}
                  {target.blockers.map((message) => <div className="hardware-blocker" key={message}>Blocked: {message}</div>)}
                  {target.warnings.map((message) => <div className="hardware-warning" key={message}>Warning: {message}</div>)}
                  {target.noop ? <small>Already matches this desired configuration.</small> : null}
                  {target.disk_label ? <small>{target.disk_label}: {gib(target.current_disk_capacity_bytes / 1024 ** 2)}</small> : null}
                </article>
              ))}
            </div>
          </div>
        ) : null}

        <div className="modal-actions">
          <button className="ghost" disabled={busy} onClick={onClose}>Cancel</button>
          {plan ? <button className="ghost" disabled={busy} onClick={() => { setPlan(null); setReviewedSpec(null); }}>Edit settings</button> : null}
          {!plan ? (
            <button className="accent" disabled={busy || targets.length > BATCH_LIMIT} onClick={() => void review()}>{busy ? "Reviewing…" : "Review changes"}</button>
          ) : (
            <button className="accent" disabled={busy || plan.can_execute_count < 1} onClick={() => void queue()}>{busy ? "Queueing…" : `Queue ${plan.can_execute_count} VM${plan.can_execute_count === 1 ? "" : "s"}`}</button>
          )}
        </div>
      </div>
    </div>
  );
}
