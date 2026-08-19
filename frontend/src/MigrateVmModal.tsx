import { FormEvent, useMemo, useState } from "react";
import { migrateVms } from "./api";
import { diskLabel } from "./format";
import type { Catalog, HostSummary, Job, VirtualMachine } from "./types";

type Props = {
  vms: VirtualMachine[];
  hosts: HostSummary[];
  catalog: Catalog | null;
  onClose: () => void;
  onQueued: (job: Job) => void;
};

export function MigrateVmModal({ vms, hosts, catalog, onClose, onQueued }: Props) {
  const clusterIds = useMemo(() => [...new Set(vms.map((vm) => vm.cluster_id).filter(Boolean))], [vms]);
  const clusterName = vms[0]?.cluster_name || "this cluster";
  const sameCluster = clusterIds.length <= 1;
  const clusterHosts = useMemo(() => {
    const clusterId = clusterIds[0] || "";
    return hosts.filter((host) => {
      if (!sameCluster) return host.connection_state.toLowerCase() !== "disconnected";
      return host.cluster_id === clusterId && host.connection_state.toLowerCase() !== "disconnected";
    });
  }, [hosts, clusterIds, sameCluster]);

  const currentHostIds = useMemo(() => new Set(vms.map((vm) => vm.host_id)), [vms]);
  const defaultHost = clusterHosts.find((host) => !currentHostIds.has(host.id))?.id || "";
  const [hostId, setHostId] = useState(defaultHost);
  const [changeStorage, setChangeStorage] = useState(false);
  const [datastoreId, setDatastoreId] = useState("");
  const [diskProvisioning, setDiskProvisioning] = useState("");
  const [changeNetwork, setChangeNetwork] = useState(false);
  const [networkId, setNetworkId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const destHost = clusterHosts.find((host) => host.id === hostId);
  const destHostId = destHost?.id || vms[0]?.host_id || "";
  const datastores = useMemo(() => {
    const rows = catalog?.datastores ?? [];
    return rows.filter((item) => !item.host_ids.length || !destHostId || item.host_ids.includes(destHostId));
  }, [catalog, destHostId]);
  const networks = useMemo(() => {
    const rows = catalog?.networks ?? [];
    return rows.filter((item) => !item.host_ids.length || !destHostId || item.host_ids.includes(destHostId));
  }, [catalog, destHostId]);

  const diskCounts = useMemo(() => {
    const counts = { thin: 0, thick: 0, mixed: 0, unknown: 0 };
    for (const vm of vms) {
      const kind = vm.disk_provisioning === "thin" || vm.disk_provisioning === "thick" || vm.disk_provisioning === "mixed" ? vm.disk_provisioning : "unknown";
      counts[kind] += 1;
    }
    return counts;
  }, [vms]);

  const hasChange = Boolean(hostId || changeStorage || diskProvisioning || changeNetwork);
  const crossCluster = Boolean(hostId && !sameCluster);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (!hasChange) {
      setError("Pick a destination host, datastore, network, or disk type");
      return;
    }
    if (crossCluster) {
      setError("Selected VMs span more than one cluster. Keep the current host, or select VMs from a single cluster.");
      return;
    }
    if (changeStorage && !datastoreId) {
      setError("Pick a destination datastore, or keep the current location");
      return;
    }
    if (changeNetwork && !networkId) {
      setError("Pick a destination network, or keep the current NICs");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const job = await migrateVms({
        vm_ids: vms.map((vm) => vm.id),
        host_id: hostId,
        datastore_id: changeStorage ? datastoreId : "",
        network_id: changeNetwork ? networkId : "",
        disk_provisioning: diskProvisioning,
        confirm: true,
      });
      onQueued(job);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Migrate failed to queue");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-back" onClick={onClose}>
      <form className="modal login-modal migrate-modal" onClick={(event) => event.stopPropagation()} onSubmit={onSubmit}>
        <h2>Migrate hosts / disks</h2>
        <p>
          Default is compute vMotion: move to another host in {clusterName} and leave datastore, NICs, and disk type
          alone. Converting Thick to Thin uses Storage vMotion on the current datastore unless you pick a new one.
        </p>
        {error ? <div className="banner bad">{error}</div> : null}
        {!sameCluster ? (
          <div className="banner">
            Selection spans {clusterIds.length} clusters. Keep the current host to convert disks in place, or select VMs
            from one cluster to move them together.
          </div>
        ) : null}

        <p className="migrate-summary">
          {vms.length} VM{vms.length === 1 ? "" : "s"} · {diskCounts.thick} thick · {diskCounts.thin} thin
          {diskCounts.mixed ? ` · ${diskCounts.mixed} mixed` : ""}
        </p>
        <ul className="migrate-vms">
          {vms.slice(0, 8).map((vm) => (
            <li key={vm.id}>
              {vm.name}
              <small>
                {vm.host_name} · {diskLabel(vm.disk_provisioning)}
              </small>
            </li>
          ))}
          {vms.length > 8 ? <li>and {vms.length - 8} more</li> : null}
        </ul>

        <label>
          Destination host
          <select value={hostId} onChange={(event) => setHostId(event.target.value)}>
            <option value="">Keep current host</option>
            {clusterHosts.map((host) => (
              <option key={host.id} value={host.id}>
                {host.name}
                {currentHostIds.has(host.id) ? " (current)" : ""}
                {` · ${host.cpu_usage_pct.toFixed(0)}% CPU · ${host.memory_usage_pct.toFixed(0)}% RAM`}
              </option>
            ))}
          </select>
        </label>

        <fieldset className="choice">
          <legend>Disk type</legend>
          <label className="check">
            <input type="radio" name="disk" checked={diskProvisioning === ""} onChange={() => setDiskProvisioning("")} />
            Keep current ({diskCounts.thick ? "Thick stays Thick" : "no conversion"})
          </label>
          <label className="check">
            <input
              type="radio"
              name="disk"
              checked={diskProvisioning === "thin"}
              onChange={() => setDiskProvisioning("thin")}
            />
            Convert to Thin — Storage vMotion; reclaim unused Thick space
          </label>
          <label className="check">
            <input
              type="radio"
              name="disk"
              checked={diskProvisioning === "thick"}
              onChange={() => setDiskProvisioning("thick")}
            />
            Convert to Thick (lazy zeroed)
          </label>
        </fieldset>

        <fieldset className="choice">
          <legend>Storage location</legend>
          <label className="check">
            <input type="radio" name="storage" checked={!changeStorage} onChange={() => setChangeStorage(false)} />
            Keep current datastore
          </label>
          <label className="check">
            <input type="radio" name="storage" checked={changeStorage} onChange={() => setChangeStorage(true)} />
            Move to another datastore
          </label>
          {changeStorage ? (
            <label>
              Datastore
              <select value={datastoreId} onChange={(event) => setDatastoreId(event.target.value)}>
                <option value="">Select datastore</option>
                {datastores.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name}
                    {item.free_bytes ? ` · ${Math.round(item.free_bytes / (1024 ** 3))} GiB free` : ""}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
        </fieldset>

        <fieldset className="choice">
          <legend>Networking</legend>
          <label className="check">
            <input type="radio" name="net" checked={!changeNetwork} onChange={() => setChangeNetwork(false)} />
            Keep current networks
          </label>
          <label className="check">
            <input type="radio" name="net" checked={changeNetwork} onChange={() => setChangeNetwork(true)} />
            Connect all NICs to one network
          </label>
          {changeNetwork ? (
            <label>
              Network
              <select value={networkId} onChange={(event) => setNetworkId(event.target.value)}>
                <option value="">Select network</option>
                {networks.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name}
                    {item.type ? ` · ${item.type}` : ""}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
        </fieldset>

        <div className="modal-actions">
          <button type="button" className="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="submit" className="accent" disabled={busy || !hasChange || crossCluster}>
            {busy ? "Queueing…" : "Queue migrate"}
          </button>
        </div>
      </form>
    </div>
  );
}
