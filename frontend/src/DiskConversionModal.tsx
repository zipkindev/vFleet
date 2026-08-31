import { useEffect, useState } from "react";
import { convertVmDisks, planDiskConversion } from "./api";
import { bytes, powerLabel } from "./format";
import type { ConnectionInfo, DiskConversionPlan, Job, VirtualMachine } from "./types";

type Props = {
  vm: VirtualMachine;
  connection: ConnectionInfo;
  onClose: () => void;
  onQueued: (job: Job) => void;
};

export function DiskConversionModal({ vm, connection, onClose, onQueued }: Props) {
  const [target, setTarget] = useState("thin");
  const [method, setMethod] = useState("auto");
  const [plan, setPlan] = useState<DiskConversionPlan | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [confirmation, setConfirmation] = useState("");

  useEffect(() => setPlan(null), [target, method]);

  async function review() {
    setBusy(true);
    setError("");
    try {
      setPlan(await planDiskConversion({ vm_id: vm.id, target, method }));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not plan disk conversion");
    } finally {
      setBusy(false);
    }
  }

  async function execute() {
    if (!plan || confirmation !== vm.name) return;
    setBusy(true);
    setError("");
    try {
      onQueued(await convertVmDisks(plan));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not queue disk conversion");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-back" onClick={() => !busy && onClose()}>
      <div className="modal disk-convert-modal" onClick={(event) => event.stopPropagation()}>
        <h2>Convert disks · {vm.name}</h2>
        <p>
          Review the exact disks and safety checks before queueing. The vSphere API method uses a storage relocation;
          SSH clones to new VMDKs and preserves the original source files.
        </p>
        {error ? <div className="banner bad">{error}</div> : null}
        <div className="login-grid">
          <label>
            Target provisioning
            <select value={target} onChange={(event) => setTarget(event.target.value)}>
              <option value="thin">Thin</option>
              <option value="lazy_zeroed_thick">Lazy-zeroed thick</option>
              <option value="eager_zeroed_thick">Eager-zeroed thick</option>
            </select>
          </label>
          <label>
            Execution method
            <select value={method} onChange={(event) => setMethod(event.target.value)}>
              <option value="auto">vSphere API (recommended)</option>
              {connection.endpoint_kind === "esxi" && connection.ssh_configured ? <option value="ssh">Verified SSH fallback</option> : null}
            </select>
          </label>
        </div>
        {!plan ? (
          <div className="modal-actions">
            <button className="ghost" onClick={onClose}>Cancel</button>
            <button className="accent" disabled={busy} onClick={() => void review()}>{busy ? "Checking…" : "Review plan"}</button>
          </div>
        ) : (
          <>
            <div className="plan-summary">
              <span className="chip">{plan.method.toUpperCase()}</span>
              <span>{powerLabel(plan.power_state)}</span>
              <span>{plan.disks.length} disk(s)</span>
              <span>Up to {bytes(plan.estimated_scratch_bytes)} temporary capacity</span>
            </div>
            <div className="disk-plan-list">
              {plan.disks.map((disk) => (
                <div key={disk.key}>
                  <strong>{disk.label}</strong>
                  <span>{bytes(disk.capacity_bytes)} · {disk.provisioning.split("_").join(" ")} · {disk.datastore_name || "datastore"}</span>
                </div>
              ))}
            </div>
            {plan.blockers.map((item) => <div className="banner bad" key={item}>{item}</div>)}
            {plan.warnings.map((item) => <div className="banner" key={item}>{item}</div>)}
            {plan.noop ? <div className="banner">No conversion is needed; every disk already matches.</div> : null}
            {plan.can_execute ? (
              <label>
                Type <strong>{vm.name}</strong> to confirm
                <input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="off" />
              </label>
            ) : null}
            <div className="modal-actions">
              <button className="ghost" disabled={busy} onClick={() => setPlan(null)}>Back</button>
              <button className="accent" disabled={busy || !plan.can_execute || confirmation !== vm.name} onClick={() => void execute()}>
                {busy ? "Queueing…" : "Queue conversion"}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
