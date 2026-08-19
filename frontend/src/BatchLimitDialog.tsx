import { useMemo, useState } from "react";
import { BATCH_LIMIT, BATCH_LIMIT_HINT, splitBatch } from "./batch";
import { BatchCount } from "./BatchCount";
import { diskLabel, gib, powerLabel } from "./format";
import type { VirtualMachine } from "./types";

type Props = {
  vms: VirtualMachine[];
  actionLabel: string;
  onCancel: () => void;
  onAccept: (included: VirtualMachine[]) => void;
};

function Row({
  vm,
  checked,
  onToggle,
  disabled,
}: {
  vm: VirtualMachine;
  checked: boolean;
  onToggle: () => void;
  disabled: boolean;
}) {
  return (
    <label className={`batch-row${checked ? " in" : ""}`}>
      <input type="checkbox" checked={checked} disabled={disabled && !checked} onChange={onToggle} />
      <span>
        <strong>{vm.name}</strong>
        <small>
          {powerLabel(vm.power_state)} · {vm.host_name || "no host"} · {vm.cpu_count} vCPU · {gib(vm.memory_mib)} ·{" "}
          {diskLabel(vm.disk_provisioning)}
          {vm.owner_key ? ` · ${vm.owner_key}` : ""}
        </small>
      </span>
    </label>
  );
}

export function BatchLimitDialog({ vms, actionLabel, onCancel, onAccept }: Props) {
  const proposed = useMemo(() => splitBatch(vms), [vms]);
  const [included, setIncluded] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(proposed.keep.map((vm) => [vm.id, true])),
  );

  const includedVms = vms.filter((vm) => included[vm.id]);
  const deferredVms = vms.filter((vm) => !included[vm.id]).sort((a, b) => a.name.localeCompare(b.name));
  const atCap = includedVms.length >= BATCH_LIMIT;

  function toggle(id: string) {
    setIncluded((current) => {
      const next = { ...current };
      if (next[id]) {
        delete next[id];
        return next;
      }
      if (Object.keys(next).length >= BATCH_LIMIT) return current;
      next[id] = true;
      return next;
    });
  }

  return (
    <div className="modal-back" onClick={onCancel}>
      <div className="modal login-modal batch-modal" onClick={(event) => event.stopPropagation()}>
        <h2>Batch is over {BATCH_LIMIT}</h2>
        <p>
          {actionLabel} is limited to {BATCH_LIMIT} VMs per job so the local relay stays responsive. Lowest-impact VMs
          (powered off, idle, small) were deferred for a second batch. This is not a vSphere vMotion cap.
        </p>
        <p className="migrate-summary">
          This job <BatchCount count={includedVms.length} /> · deferred {deferredVms.length} of {vms.length}
        </p>
        <p className="batch-hint" title={BATCH_LIMIT_HINT}>
          Uncheck a kept VM to free a slot, then check a deferred VM to include it instead.
        </p>

        <div className="batch-cols">
          <section>
            <h3>First batch ({includedVms.length})</h3>
            <div className="batch-list">
              {includedVms
                .slice()
                .sort((a, b) => a.name.localeCompare(b.name))
                .map((vm) => (
                  <Row key={vm.id} vm={vm} checked onToggle={() => toggle(vm.id)} disabled={false} />
                ))}
            </div>
          </section>
          <section>
            <h3>Deferred ({deferredVms.length})</h3>
            <div className="batch-list">
              {deferredVms.map((vm) => (
                <Row key={vm.id} vm={vm} checked={false} onToggle={() => toggle(vm.id)} disabled={atCap} />
              ))}
            </div>
          </section>
        </div>

        <div className="modal-actions">
          <button type="button" className="ghost" onClick={onCancel}>
            Cancel
          </button>
          <button
            type="button"
            className="accent"
            disabled={includedVms.length === 0}
            onClick={() => onAccept(includedVms)}
          >
            Use {includedVms.length} in this job
          </button>
        </div>
      </div>
    </div>
  );
}
