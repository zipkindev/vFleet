import { useEffect, useMemo, useState } from "react";
import { convertVmDisks, planDiskConversion } from "./api";
import { bytes, powerLabel } from "./format";
import type { ConnectionInfo, DiskConversionPlan, Job, VirtualMachine } from "./types";

type PlanRow = {
  vm: VirtualMachine;
  plan: DiskConversionPlan | null;
  error: string;
};

export type DiskConversionQueueResult = {
  jobs: Job[];
  failures: string[];
  queuedVmIds: string[];
};

type Props = {
  vms: VirtualMachine[];
  connection: ConnectionInfo;
  onClose: () => void;
  onQueued: (result: DiskConversionQueueResult) => void;
};

export function DiskConversionModal({ vms, connection, onClose, onQueued }: Props) {
  const [target, setTarget] = useState("thin");
  const [method, setMethod] = useState("auto");
  const [rows, setRows] = useState<PlanRow[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [showConfirmation, setShowConfirmation] = useState(false);

  useEffect(() => {
    setRows(null);
    setShowConfirmation(false);
  }, [target, method]);

  const executable = useMemo(
    () => (rows ?? []).filter((row): row is PlanRow & { plan: DiskConversionPlan } => Boolean(row.plan?.can_execute)),
    [rows],
  );
  const scratchBytes = executable.reduce((sum, row) => sum + row.plan.estimated_scratch_bytes, 0);
  const largestScratchBytes = executable.reduce((largest, row) => Math.max(largest, row.plan.estimated_scratch_bytes), 0);
  const preservesSources = executable.some((row) => row.plan.method === "ssh");
  const methodNames = new Set(executable.map((row) => row.plan.method));
  const methodLabel = methodNames.size > 1
    ? "Mixed"
    : methodNames.has("ssh") ? "Verified SSH" : "vSphere API";
  const targetLabel = target === "thin"
    ? "Thin"
    : target === "eager_zeroed_thick" ? "Eager-zeroed thick" : "Lazy-zeroed thick";
  const capacityLabel = preservesSources
    ? `${bytes(scratchBytes)} additional`
    : `${bytes(largestScratchBytes)} temporary per job`;

  async function review() {
    setBusy(true);
    setError("");
    setShowConfirmation(false);
    setRows([]);
    const next: PlanRow[] = [];
    for (const vm of vms) {
      try {
        const plan = await planDiskConversion({ vm_id: vm.id, target, method });
        next.push({ vm, plan, error: "" });
      } catch (err) {
        next.push({ vm, plan: null, error: err instanceof Error ? err.message : "Could not plan disk conversion" });
      }
      setRows([...next]);
    }
    setBusy(false);
  }

  async function execute() {
    if (executable.length === 0 || !showConfirmation) return;
    setBusy(true);
    setError("");
    const jobs: Job[] = [];
    const failures: string[] = [];
    const queuedVmIds: string[] = [];
    for (const row of executable) {
      try {
        jobs.push(await convertVmDisks(row.plan));
        queuedVmIds.push(row.vm.id);
      } catch (err) {
        failures.push(`${row.vm.name}: ${err instanceof Error ? err.message : "Could not queue conversion"}`);
      }
    }
    setBusy(false);
    setShowConfirmation(false);
    if (jobs.length === 0) {
      setError(failures.join("; ") || "No conversions were queued");
      return;
    }
    onQueued({ jobs, failures, queuedVmIds });
  }

  const batch = vms.length > 1;
  const planned = rows !== null && rows.length === vms.length;

  return (
    <>
      <div className="modal-back" onClick={() => !busy && onClose()}>
        <div className="modal disk-convert-modal" onClick={(event) => event.stopPropagation()}>
        <h2>{batch ? `Convert disks · ${vms.length} selected VMs` : `Convert disks · ${vms[0]?.name ?? "VM"}`}</h2>
        <p>
          Each VM is safety-checked and queued as an independent persistent job. The vSphere API uses storage relocation;
          SSH clones new VMDKs and preserves the original source files.
        </p>
        {error ? <div className="banner bad">{error}</div> : null}
        <div className="disk-convert-settings">
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

        {rows === null ? (
          <div className="modal-actions">
            <button className="ghost" onClick={onClose}>Cancel</button>
            <button className="accent" disabled={busy} onClick={() => void review()}>{busy ? "Checking…" : `Review ${vms.length} plan${batch ? "s" : ""}`}</button>
          </div>
        ) : (
          <>
            <div className="plan-summary">
              <span className={`chip ${executable.length ? "ok" : "warm"}`}>{executable.length} ready</span>
              <span>{Math.max(0, rows.length - executable.length)} skipped or blocked</span>
              <span>
                {preservesSources
                  ? `Up to ${bytes(scratchBytes)} additional capacity while source disks are preserved`
                  : `Up to ${bytes(largestScratchBytes)} temporary per job · jobs run one at a time`}
              </span>
              {busy && !planned ? <span>Checking {rows.length + 1} of {vms.length}…</span> : null}
            </div>

            <div className="disk-batch-list">
              {rows.map((row) => (
                <section className="disk-batch-card" key={row.vm.id}>
                  <header>
                    <div>
                      <strong>{row.vm.name}</strong>
                      <small>{powerLabel(row.vm.power_state)} · {row.vm.host_name || "no host"}</small>
                    </div>
                    <span className={`chip ${row.plan?.can_execute ? "ok" : "warm"}`}>
                      {row.plan?.can_execute ? "Ready" : row.plan?.noop ? "Already matches" : "Blocked"}
                    </span>
                  </header>
                  {row.error ? <div className="banner bad">{row.error}</div> : null}
                  {row.plan ? (
                    <>
                      <div className="disk-card-meta">
                        <span>{row.plan.method.toUpperCase()}</span>
                        <span>{row.plan.disks.length} disk(s)</span>
                        <span>Up to {bytes(row.plan.estimated_scratch_bytes)} temporary</span>
                      </div>
                      <div className="disk-plan-list">
                        {row.plan.disks.map((disk) => (
                          <div key={disk.key}>
                            <strong>{disk.label}</strong>
                            <span>{bytes(disk.capacity_bytes)} · {disk.provisioning.split("_").join(" ")} · {disk.datastore_name || "datastore"}</span>
                          </div>
                        ))}
                      </div>
                      {row.plan.blockers.map((item) => <div className="banner bad" key={item}>{item}</div>)}
                      {row.plan.warnings.map((item) => <div className="banner" key={item}>{item}</div>)}
                      {row.plan.noop ? <div className="banner">No conversion is needed; every disk already matches.</div> : null}
                    </>
                  ) : null}
                </section>
              ))}
            </div>

            <div className="modal-actions">
              <button className="ghost" disabled={busy} onClick={() => setRows(null)}>Back</button>
              <button
                className="accent"
                disabled={busy || !planned || executable.length === 0}
                onClick={() => setShowConfirmation(true)}
              >
                {`Queue ${executable.length} conversion${executable.length === 1 ? "" : "s"}`}
              </button>
            </div>
          </>
        )}
        </div>
      </div>

      {showConfirmation ? (
        <div className="modal-back disk-confirm-back" onClick={() => !busy && setShowConfirmation(false)}>
          <div
            className="modal disk-confirm-modal"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="disk-confirm-title"
            aria-describedby="disk-confirm-description"
            onClick={(event) => event.stopPropagation()}
          >
            <h2 id="disk-confirm-title">Confirm disk conversions</h2>
            <p id="disk-confirm-description">
              Queue {executable.length} independent conversion {executable.length === 1 ? "job" : "jobs"}? Jobs run one at a time and each VM plan is revalidated before execution.
            </p>

            <div className="disk-confirm-grid" aria-label="Conversion summary">
              <div><span>Virtual machines</span><strong>{executable.length}</strong></div>
              <div><span>Target</span><strong>{targetLabel}</strong></div>
              <div><span>Method</span><strong>{methodLabel}</strong></div>
              <div><span>Capacity impact</span><strong>{capacityLabel}</strong></div>
            </div>

            <div className="disk-confirm-vms">
              <span>Ready to queue</span>
              <ul>
                {executable.map((row) => <li key={row.vm.id}>{row.vm.name}</li>)}
              </ul>
            </div>

            <div className="banner warm disk-confirm-warning">
              This changes disk provisioning on the selected VMs. Closing or canceling this popup queues nothing.
            </div>

            <div className="modal-actions">
              <button className="ghost" disabled={busy} onClick={() => setShowConfirmation(false)}>Cancel</button>
              <button className="accent" disabled={busy} onClick={() => void execute()} autoFocus>
                {busy
                  ? "Queueing…"
                  : `Confirm & queue ${executable.length} conversion${executable.length === 1 ? "" : "s"}`}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}
