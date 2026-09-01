import { useEffect, useMemo, useState } from "react";
import { cleanupStorageReconciliation, planStorageReconciliation } from "./api";
import { bytes, powerLabel } from "./format";
import type {
  Job,
  StorageReconciliationCandidate,
  StorageReconciliationReport,
  VirtualMachine,
} from "./types";

type Props = {
  vms: VirtualMachine[];
  inventoryVms: VirtualMachine[];
  onClose: () => void;
  onQueued: (jobs: Job[], failures: string[]) => void;
  onDeployTools: (vms: VirtualMachine[]) => void;
};

function kindLabel(candidate: StorageReconciliationCandidate): string {
  if (candidate.kind === "preserved_source_disk") return "Preserved conversion source";
  if (candidate.kind === "unregistered_vm_directory") return "Unregistered VM directory";
  return "Unattached virtual disk";
}

export function StorageReconciliationModal({ vms, inventoryVms, onClose, onQueued, onDeployTools }: Props) {
  const [report, setReport] = useState<StorageReconciliationReport | null>(null);
  const [selected, setSelected] = useState<Record<string, boolean>>({});
  const [manual, setManual] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [confirming, setConfirming] = useState(false);
  const vmIds = useMemo(() => vms.map((vm) => vm.id), [vms]);
  const allInventory = vmIds.length === 0;

  async function review() {
    setBusy(true);
    setError("");
    setConfirming(false);
    try {
      const next = await planStorageReconciliation({
        vm_ids: vmIds,
        include_unregistered_directories: allInventory,
      });
      setReport(next);
      setSelected({});
      setManual({});
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not reconcile storage inventory");
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    void review();
    // The scope is fixed for the lifetime of this modal.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const chosen = (report?.candidates ?? []).filter((candidate) => selected[candidate.id]);
  const blockedSelection = chosen.some(
    (candidate) => candidate.validation_status === "blocked" || (!candidate.can_delete && !manual[candidate.id]),
  );
  const cleanupBytes = chosen.reduce((sum, candidate) => sum + candidate.size, 0);
  const toolsTargets = (report?.vm_statuses ?? [])
    .filter((status) => !status.tools_running && status.power_state === "POWERED_ON")
    .map((status) => inventoryVms.find((vm) => vm.id === status.vm_id))
    .filter((vm): vm is VirtualMachine => Boolean(vm));

  function toggle(candidate: StorageReconciliationCandidate) {
    if (candidate.validation_status === "blocked") return;
    setSelected((current) => ({ ...current, [candidate.id]: !current[candidate.id] }));
  }

  async function cleanup() {
    if (!report || chosen.length === 0 || blockedSelection) return;
    setBusy(true);
    setError("");
    try {
      const result = await cleanupStorageReconciliation({
        vm_ids: vmIds,
        include_unregistered_directories: allInventory,
        candidate_ids: chosen.map((candidate) => candidate.id),
        manual_validated_candidate_ids: chosen.filter((candidate) => manual[candidate.id]).map((candidate) => candidate.id),
        plan_token: report.plan_token,
      });
      if (result.jobs.length === 0) {
        setError(result.failures.join("; ") || "No cleanup jobs were queued");
        setConfirming(false);
        await review();
        return;
      }
      onQueued(result.jobs, result.failures);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not queue storage cleanup");
      setConfirming(false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="modal-back" onClick={() => !busy && onClose()}>
        <div className="modal storage-reconcile-modal" onClick={(event) => event.stopPropagation()}>
          <h2>{allInventory ? "Reconcile storage and inventory" : `Reconcile storage · ${vms.length} VM${vms.length === 1 ? "" : "s"}`}</h2>
          <p>
            Compares current VM disk attachments with completed conversion jobs and datastore contents. High-confidence
            preserved sources are separated from uncertain disks and unregistered VM directories.
          </p>
          {error ? <div className="banner bad">{error}</div> : null}
          {busy && !report ? <div className="banner">Refreshing inventory and scanning datastore folders…</div> : null}
          {report?.inventory_stale ? (
            <div className="banner bad">Inventory is stale. Reconnect and refresh before cleanup; no candidate can be queued.</div>
          ) : null}
          {report?.warnings.map((warning) => <div className="banner warm" key={warning}>{warning}</div>)}

          {report ? (
            <>
              <div className="plan-summary">
                <span className={`chip ${report.candidates.length ? "warm" : "ok"}`}>{report.candidates.length} candidate(s)</span>
                <span>{report.scanned_directories} datastore folder(s) inspected</span>
                <span>{report.vm_statuses.filter((status) => status.boot_validated).length} post-boot validation(s) automated</span>
              </div>

              {report.vm_statuses.map((status) => (
                <section className="reconcile-vm-status" key={status.vm_id}>
                  <div>
                    <strong>{status.vm_name}</strong>
                    <small>{powerLabel(status.power_state)} · {status.tools_running ? "VMware Tools running" : "VMware Tools not running"}</small>
                    <p>{status.message}</p>
                  </div>
                  {!status.tools_running && status.power_state === "POWERED_ON" ? (
                    <button
                      className="ghost compact"
                      onClick={() => {
                        const vm = inventoryVms.find((item) => item.id === status.vm_id);
                        if (vm) onDeployTools([vm]);
                      }}
                    >
                      Deploy VMware Tools
                    </button>
                  ) : null}
                </section>
              ))}

              <div className="disk-batch-list storage-reconcile-list">
                {report.candidates.map((candidate) => {
                  const needsManual = !candidate.can_delete && candidate.validation_status === "manual_required";
                  return (
                    <section className="disk-batch-card" key={candidate.id}>
                      <header>
                        <label className="check">
                          <input
                            type="checkbox"
                            checked={Boolean(selected[candidate.id])}
                            disabled={candidate.validation_status === "blocked" || report.inventory_stale}
                            onChange={() => toggle(candidate)}
                          />
                          <span>
                            <strong>{candidate.datastore_name}/{candidate.path}</strong>
                            <small>{kindLabel(candidate)} · {bytes(candidate.size)}</small>
                          </span>
                        </label>
                        <span className={`chip ${candidate.confidence === "high" ? "ok" : "warm"}`}>
                          {candidate.confidence === "high" ? "High confidence" : "Review required"}
                        </span>
                      </header>
                      <p>{candidate.reason}</p>
                      {candidate.vm_name ? <small className="sub">VM: {candidate.vm_name}</small> : null}
                      {candidate.warning ? <div className={`banner ${candidate.validation_status === "blocked" ? "bad" : "warm"}`}>{candidate.warning}</div> : null}
                      {needsManual ? (
                        <label className="check manual-reconcile-check">
                          <input
                            type="checkbox"
                            checked={Boolean(manual[candidate.id])}
                            onChange={(event) => setManual((current) => ({ ...current, [candidate.id]: event.target.checked }))}
                          />
                          I validated the VM after boot, or verified this unattached item is not needed for recovery.
                        </label>
                      ) : candidate.can_delete ? (
                        <div className="banner">Current attachment and post-boot VMware Tools checks passed.</div>
                      ) : null}
                    </section>
                  );
                })}
              </div>

              {report.candidates.length === 0 ? (
                <p className="empty pad">No preserved conversion sources, unattached VMDKs, or unregistered VM folders were found in this scope.</p>
              ) : null}

              <div className="modal-actions">
                <button className="ghost" disabled={busy} onClick={onClose}>Close</button>
                {toolsTargets.length ? (
                  <button className="ghost" disabled={busy} onClick={() => onDeployTools(toolsTargets)}>
                    Deploy Tools to {toolsTargets.length} VM{toolsTargets.length === 1 ? "" : "s"}
                  </button>
                ) : null}
                <button className="ghost" disabled={busy} onClick={() => void review()}>{busy ? "Scanning…" : "Run again"}</button>
                <button
                  className="danger"
                  disabled={busy || chosen.length === 0 || blockedSelection || report.inventory_stale}
                  onClick={() => setConfirming(true)}
                >
                  Review cleanup ({chosen.length})
                </button>
              </div>
              {blockedSelection ? (
                <div className="banner bad">Every selected review-only candidate needs its manual validation acknowledgement before cleanup.</div>
              ) : null}
            </>
          ) : (
            <div className="modal-actions"><button className="ghost" onClick={onClose}>Close</button></div>
          )}
        </div>
      </div>

      {confirming ? (
        <div className="modal-back disk-confirm-back" onClick={() => !busy && setConfirming(false)}>
          <div className="modal disk-confirm-modal" role="alertdialog" aria-modal="true" onClick={(event) => event.stopPropagation()}>
            <h2>Confirm reconciled storage cleanup</h2>
            <p>Permanently delete {chosen.length} detached item{chosen.length === 1 ? "" : "s"} totaling approximately {bytes(cleanupBytes)}?</p>
            <ul>
              {chosen.map((candidate) => <li key={candidate.id}>{candidate.datastore_name}/{candidate.path}</li>)}
            </ul>
            <div className="banner bad">This cannot be undone. The server will rebuild and compare the reconciliation plan before queueing deletion.</div>
            <div className="modal-actions">
              <button className="ghost" disabled={busy} onClick={() => setConfirming(false)}>Cancel</button>
              <button className="danger" disabled={busy} onClick={() => void cleanup()} autoFocus>
                {busy ? "Queueing…" : `Confirm & queue ${chosen.length} cleanup${chosen.length === 1 ? "" : "s"}`}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}
